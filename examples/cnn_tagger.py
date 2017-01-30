from __future__ import print_function
from timeit import default_timer as timer
import plac

from thinc.neural.id2vec import Embed
from thinc.neural.vec2vec import Model, ReLu, Softmax
from thinc.neural._classes.convolution import ExtractWindow
from thinc.neural._classes.maxout import Maxout

from thinc.loss import categorical_crossentropy
from thinc.api import layerize, chain, clone
from thinc.neural.util import flatten_sequences

from thinc.extra.datasets import ancora_pos_tags



def main(width=64, vector_length=64):
    train_data, check_data, nr_tag = ancora_pos_tags()

    with Model.define_operators({'**': clone, '>>': chain}):
        model = (
            layerize(flatten_sequences)
            >> Embed(width, vector_length)
            >> ExtractWindow(nW=1)
            >> Maxout(128)
            >> ExtractWindow(nW=1)
            >> Maxout(128)
            >> ExtractWindow(nW=1)
            >> Maxout(128)
            >> Softmax(nr_tag))

    train_X, train_y = zip(*train_data)
    print("NR vector", max(max(seq) for seq in train_X))
    dev_X, dev_y = zip(*check_data)
    dev_y = model.ops.flatten(dev_y)
    n_train = sum(len(x) for x in train_X)
    with model.begin_training(train_X, train_y) as (trainer, optimizer):
        trainer.batch_size = 4
        trainer.nb_epoch = 20
        trainer.dropout = 0.9
        trainer.dropout_decay = 1e-4
        epoch_times = [timer()]
        def track_progress():
            start = timer()
            acc = model.evaluate(dev_X, dev_y)
            end = timer()
            with model.use_params(optimizer.averages):
                avg_acc = model.evaluate(dev_X, dev_y)
            stats = (
                acc,
                avg_acc,
                float(n_train) / (end-epoch_times[-1]),
                float(dev_y.shape[0]) / (end-start))
            print("%.3f (%.3f) acc, %d wps train, %d wps run" % stats)
            epoch_times.append(end)
        trainer.each_epoch.append(track_progress)
        for X, y in trainer.iterate(train_X, train_y):
            y = model.ops.flatten(y)
            yh, backprop = model.begin_update(X, drop=trainer.dropout)
            d_loss, loss = categorical_crossentropy(yh, y)
            optimizer.set_loss(loss)
            backprop(d_loss, optimizer)
    with model.use_params(optimizer.averages):
        print(model.evaluate(dev_X, dev_y))
 

if __name__ == '__main__':
    if 1:
        plac.call(main)
    else:
        import cProfile
        import pstats
        cProfile.runctx("plac.call(main)", globals(), locals(), "Profile.prof")
        s = pstats.Stats("Profile.prof")
        s.strip_dirs().sort_stats("time").print_stats()
