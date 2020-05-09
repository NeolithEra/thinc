from typing import Any
import contextlib
from io import BytesIO
import srsly

try:
    import torch.autograd
    import torch.optim
    import torch
except ImportError:  # pragma: no cover
    pass

from ..util import torch2xp, xp2torch, convert_recursive
from ..backends import get_current_ops, get_array_ops
from ..types import ArgsKwargs
from .shim import Shim


class PyTorchShim(Shim):
    """Interface between a PyTorch model and a Thinc Model. This container is
    *not* a Thinc Model subclass itself.
    """

    def __call__(self, inputs, is_train):
        if is_train:
            return self.begin_update(inputs)
        else:
            return self.predict(inputs), lambda a: ...

    def predict(self, inputs: ArgsKwargs) -> Any:
        """Pass inputs through to the underlying PyTorch model, and return the
        output. No conversions are performed. The PyTorch model is set into
        evaluation mode.
        """
        self._model.eval()
        with torch.no_grad():
            outputs = self._model(*inputs.args, **inputs.kwargs)
        self._model.train()
        return outputs

    def begin_update(self, inputs: ArgsKwargs):
        """Pass the inputs through to the underlying PyTorch model, keeping
        track of which items in the input are tensors requiring gradients.
        If the model returns a single value, it is converted into a one-element tuple. Return the outputs and a callback to backpropagate.  """
        self._model.train()
        output = self._model(*inputs.args, **inputs.kwargs)

        def backprop(grads):
            torch.autograd.backward(*grads.args, **grads.kwargs)
            return convert_recursive(
                lambda x: hasattr(x, "grad"), lambda x: x.grad, inputs
            )

        return output, backprop

    def finish_update(self, optimizer):
        pytorch_opt = self._get_pytorch_optimizer(optimizer)
        if getattr(optimizer, "grad_clip", None):
            torch.nn.utils.clip_grad_norm_(
                self._model.parameters(), optimizer.grad_clip
            )
        pytorch_opt.step()
        pytorch_opt.zero_grad()
        self._update_pytorch_averages(optimizer)

    def _get_pytorch_optimizer(self, sgd):
        args = {
            "lr": sgd.learn_rate,
            "weight_decay": sgd.L2
        }

        if sgd.b1 != 0 and sgd.b2 != 0:
            args["betas"] = (sgd.b1, sgd.b2)
            args["eps"] = sgd.eps
            if sgd.L2_is_weight_decay:
                cls = torch.optim.AdamW
            else:
                cls = torch.optim.Adam
        else:
            cls = thinc.optim.SGD
            if sgd.b2 == 0:
                args["momentum"] = sgd.b1
        if self._optimizer is None:
            self._optimizer = cls(self._model.parameters(), **args)
        else:
            for param_group in self._optimizer.param_groups:
                param_group.update(args)
        return self._optimizer

    @contextlib.contextmanager
    def use_params(self, params):
        key_prefix = f"pytorch_{self.id}_"
        state_dict = {}
        for k, v in params.items():
            if hasattr(k, "startswith") and k.startswith(key_prefix):
                state_dict[k.replace(key_prefix, "")] = xp2torch(v)
        if state_dict:
            backup = {k: v.clone() for k, v in self._model.state_dict().items()}
            self._model.load_state_dict(state_dict)
            yield
            self._model.load_state_dict(backup)
        else:
            yield

    def _update_pytorch_averages(self, sgd, *, init_steps=1):
        if getattr(sgd, "averages", None) is None:
            return
        # Collect parameters if we don't have them
        for name, param in self._model.state_dict().items():
            key = f"pytorch_{self.id}_{name}"
            sgd.nr_update[key] += 1
            xp_param = torch2xp(param)
            ops = get_array_ops(xp_param)
            if key in sgd.averages:
                ops.update_averages(sgd.averages[key], xp_param, sgd.nr_update[key])
            else:
                sgd.averages[key] = xp_param.copy()
                sgd.nr_update[key] = init_steps

    def to_device(self, device):  # pragma: no cover
        if device == "cpu":
            self._model.cpu()
        else:
            self._model.cuda(device)

    def to_bytes(self):
        filelike = BytesIO()
        torch.save(self._model.state_dict(), filelike)
        filelike.seek(0)
        weights_bytes = filelike.getvalue()
        msg = {"config": self.cfg, "state": weights_bytes}
        return srsly.msgpack_dumps(msg)

    def from_bytes(self, bytes_data):
        ops = get_current_ops()
        msg = srsly.msgpack_loads(bytes_data)
        self.cfg = msg["config"]
        filelike = BytesIO(msg["state"])
        filelike.seek(0)
        if ops.device_type == "cpu":
            map_location = "cpu"
        else:  # pragma: no cover
            device_id = torch.cuda.current_device()
            map_location = "cuda:%d" % device_id
        self._model.load_state_dict(torch.load(filelike, map_location=map_location))
        self._model.to(map_location)
        return self
