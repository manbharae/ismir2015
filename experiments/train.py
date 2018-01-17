#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
Trains a neural network for singing voice detection.

For usage information, call with --help.

Author: Jan Schlüter
"""

from __future__ import print_function

import sys
import os
import io
from argparse import ArgumentParser

import numpy as np
import theano
import theano.tensor as T
floatX = theano.config.floatX
import lasagne

from progress import progress
from simplecache import cached
import audio
import znorm
from labels import create_aligned_targets
import model
import augment
import config


def opts_parser():
    descr = "Trains a neural network for singing voice detection."
    parser = ArgumentParser(description=descr)
    parser.add_argument('modelfile', metavar='MODELFILE',
            type=str,
            help='File to save the learned weights to (.npz format)')
    parser.add_argument('--dataset',
            type=str, default='jamendo',
            help='Name of the dataset to use (default: %(default)s)')
    parser.add_argument('--augment',
            action='store_true', default=True,
            help='Perform train-time data augmentation (enabled by default)')
    parser.add_argument('--no-augment',
            action='store_false', dest='augment',
            help='Disable train-time data augmentation')
    parser.add_argument('--validate',
            action='store_true', default=False,
            help='Monitor validation loss (disabled by default)')
    parser.add_argument('--no-validate',
            action='store_false', dest='validate',
            help='Disable monitoring validation loss (disabled by default)')
    parser.add_argument('--save-errors',
            action='store_true', default=False,
            help='If given, save error log in {MODELFILE%%.npz}.err.npz.')
    parser.add_argument('--cache-spectra', metavar='DIR',
            type=str, default=None,
            help='Store spectra in the given directory (disabled by default)')
    parser.add_argument('--load-spectra',
            choices=('memory', 'memmap', 'on-demand'), default='memory',
            help='By default, spectrograms are loaded to memory. Large '
                 'datasets can be read as memory-mapped files (if not '
                 'exceeding the allowable number of open files) or read '
                 'on-demand. The latter two require --cache-spectra.')
    parser.add_argument('--vars', metavar='FILE',
            action='append', type=str,
            default=[os.path.join(os.path.dirname(__file__), 'defaults.vars')],
            help='Reads configuration variables from a FILE of KEY=VALUE '
                 'lines. Can be given multiple times, settings from later '
                 'files overriding earlier ones. Will read defaults.vars, '
                 'then files given here.')
    parser.add_argument('--var', metavar='KEY=VALUE',
            action='append', type=str,
            help='Set the configuration variable KEY to VALUE. Overrides '
                 'settings from --vars options. Can be given multiple times.')
    return parser

def main():
    # parse command line
    parser = opts_parser()
    options = parser.parse_args()
    modelfile = options.modelfile
    if options.load_spectra != 'memory' and not options.cache_spectra:
        parser.error('option --load-spectra=%s requires --cache-spectra' %
                     options.load_spectra)

    # read configuration files and immediate settings
    cfg = {}
    for fn in options.vars:
        cfg.update(config.parse_config_file(fn))
    cfg.update(config.parse_variable_assignments(options.var))

    # read some settings into local variables
    sample_rate = cfg['sample_rate']
    frame_len = cfg['frame_len']
    fps = cfg['fps']
    mel_bands = cfg['mel_bands']
    mel_min = cfg['mel_min']
    mel_max = cfg['mel_max']
    blocklen = cfg['blocklen']
    batchsize = cfg['batchsize']
    
    bin_nyquist = frame_len // 2 + 1
    bin_mel_max = bin_nyquist * 2 * mel_max // sample_rate

    # prepare dataset
    datadir = os.path.join(os.path.dirname(__file__),
                           os.path.pardir, 'datasets', options.dataset)

    # - load filelist
    with io.open(os.path.join(datadir, 'filelists', 'train')) as f:
        filelist = [l.rstrip() for l in f if l.rstrip()]
    if options.validate:
        with io.open(os.path.join(datadir, 'filelists', 'valid')) as f:
            filelist_val = [l.rstrip() for l in f if l.rstrip()]
        filelist.extend(filelist_val)
    else:
        filelist_val = []

    # - compute spectra
    print("Computing%s spectra..." %
          (" or loading" if options.cache_spectra else ""))
    spects = []
    for fn in progress(filelist, 'File '):
        cache_fn = (options.cache_spectra and
                    os.path.join(options.cache_spectra, fn + '.npy'))
        spects.append(cached(cache_fn,
                             audio.extract_spect,
                             os.path.join(datadir, 'audio', fn),
                             sample_rate, frame_len, fps,
                             loading_mode=options.load_spectra))

    # - load and convert corresponding labels
    print("Loading labels...")
    labels = []
    for fn, spect in zip(filelist, spects):
        fn = os.path.join(datadir, 'labels', fn.rsplit('.', 1)[0] + '.lab')
        with io.open(fn) as f:
            segments = [l.rstrip().split() for l in f if l.rstrip()]
        segments = [(float(start), float(end), label == 'sing')
                    for start, end, label in segments]
        timestamps = np.arange(len(spect)) / float(fps)
        labels.append(create_aligned_targets(segments, timestamps, np.bool))

    # - split off validation data, if needed
    if options.validate:
        spects_val = spects[-len(filelist_val):]
        spects = spects[:-len(filelist_val)]
        labels_val = labels[-len(filelist_val):]
        labels = labels[:-len(filelist_val)]

    # - prepare training data generator
    print("Preparing training data feed...")
    if not options.augment:
        # Without augmentation, we just create a generator that returns
        # mini-batches of random excerpts
        batches = augment.grab_random_excerpts(
            spects, labels, batchsize, blocklen, bin_mel_max)
        batches = augment.generate_in_background(
                [batches], num_cached=15)
    else:
        # For time stretching and pitch shifting, it pays off to preapply the
        # spline filter to each input spectrogram, so it does not need to be
        # applied to each mini-batch later.
        spline_order = cfg['spline_order']
        if spline_order > 1 and options.load_spectra == 'memory':
            from scipy.ndimage import spline_filter
            spects = [spline_filter(spect, spline_order).astype(floatX)
                      for spect in spects]
            prefiltered = True
        else:
            prefiltered = False

        # We define a function to create the mini-batch generator. This allows
        # us to easily create multiple generators for multithreading if needed.
        def create_datafeed(spects, labels):
            # With augmentation, as we want to apply random time-stretching,
            # we request longer excerpts than we finally need to return.
            max_stretch = cfg['max_stretch']
            batches = augment.grab_random_excerpts(
                    spects, labels, batchsize=batchsize,
                    frames=int(blocklen / (1 - max_stretch)))

            # We wrap the generator in another one that applies random time
            # stretching and pitch shifting, keeping a given number of frames
            # and bins only.
            max_shift = cfg['max_shift']
            batches = augment.apply_random_stretch_shift(
                    batches, max_stretch, max_shift,
                    keep_frames=blocklen, keep_bins=bin_mel_max,
                    order=spline_order, prefiltered=prefiltered)

            # We apply random frequency filters
            max_db = cfg['max_db']
            batches = augment.apply_random_filters(batches, mel_max, max_db)

            return batches

        # We start the mini-batch generator and augmenter in one or more
        # background threads or processes (unless disabled).
        bg_threads = cfg['bg_threads']
        bg_processes = cfg['bg_processes']
        if not bg_threads and not bg_processes:
            # no background processing: just create a single generator
            batches = create_datafeed(spects, labels)
        elif bg_threads:
            # multithreading: create a separate generator per thread
            batches = augment.generate_in_background(
                    [create_datafeed(spects, labels)
                     for _ in range(bg_threads)],
                    num_cached=bg_threads * 5)
        elif bg_processes:
            # multiprocessing: single generator is forked along with processes
            batches = augment.generate_in_background(
                    [create_datafeed(spects, labels)] * bg_processes,
                    num_cached=bg_processes * 25,
                    in_processes=True)


    print("Preparing training function...")
    # instantiate neural network
    input_var = T.tensor3('input')
    inputs = input_var.dimshuffle(0, 'x', 1, 2)  # insert "channels" dimension
    network = model.architecture(inputs, (None, 1, blocklen, bin_mel_max), cfg)
    print("- %d layers (%d with weights), %f mio params" %
          (len(lasagne.layers.get_all_layers(network)),
           sum(hasattr(l, 'W') for l in lasagne.layers.get_all_layers(network)),
           lasagne.layers.count_params(network, trainable=True) / 1e6))
    print("- weight shapes: %r" % [l.W.get_value().shape
           for l in lasagne.layers.get_all_layers(network)
           if hasattr(l, 'W') and hasattr(l.W, 'get_value')])

    # create cost expression
    target_var = T.vector('targets')
    targets = (0.02 + 0.96 * target_var)  # map 0 -> 0.02, 1 -> 0.98
    targets = targets.dimshuffle(0, 'x')  # turn into column vector
    outputs = lasagne.layers.get_output(network, deterministic=False)
    cost = T.mean(lasagne.objectives.binary_crossentropy(outputs, targets))
    if cfg.get('l2_decay', 0):
        cost_l2 = lasagne.regularization.regularize_network_params(
                network, lasagne.regularization.l2) * cfg['l2_decay']
    else:
        cost_l2 = 0

    # prepare and compile training function
    params = lasagne.layers.get_all_params(network, trainable=True)
    initial_eta = cfg['initial_eta']
    eta_decay = cfg['eta_decay']
    eta_decay_every = cfg.get('eta_decay_every', 1)
    momentum = cfg['momentum']
    if cfg['learn_scheme'] == 'nesterov':
        learn_scheme = lasagne.updates.nesterov_momentum
    elif cfg['learn_scheme'] == 'momentum':
        learn_scheme = lasagne.update.momentum
    elif cfg['learn_scheme'] == 'adam':
        learn_scheme = lasagne.updates.adam
    else:
        raise ValueError('Unknown learn_scheme=%s' % cfg['learn_scheme'])
    eta = theano.shared(lasagne.utils.floatX(initial_eta))
    updates = learn_scheme(cost + cost_l2, params, eta, momentum)
    print("Compiling training function...")
    train_fn = theano.function([input_var, target_var], cost, updates=updates)

    # prepare and compile validation function, if requested
    if options.validate:
        print("Compiling validation function...")
        import model_to_fcn
        network_test = model_to_fcn.model_to_fcn(network, allow_unlink=False)
        outputs_test = lasagne.layers.get_output(network_test,
                                                 deterministic=True)
        cost_test = T.mean(lasagne.objectives.binary_crossentropy(outputs_test,
                                                                  targets))
        val_fn = theano.function([input_var, target_var],
                                 [cost_test, outputs_test])

    # run training loop
    print("Training:")
    epochs = cfg['epochs']
    epochsize = cfg['epochsize']
    batches = iter(batches)
    if options.save_errors:
        errors = []
    for epoch in range(epochs):
        # actual training
        err = 0
        for batch in progress(
                range(epochsize), min_delay=.5,
                desc='Epoch %d/%d: Batch ' % (epoch + 1, epochs)):
            err += train_fn(*next(batches))
            if not np.isfinite(err):
                print("\nEncountered NaN loss in training. Aborting.")
                sys.exit(1)
        if eta_decay != 1 and (epoch + 1) % eta_decay_every == 0:
            eta.set_value(eta.get_value() * lasagne.utils.floatX(eta_decay))

        # report training loss
        print("Train loss: %.3f" % (err / epochsize))
        if options.save_errors:
            errors.append(err / epochsize)

        # compute and report validation loss, if requested
        if options.validate:
            val_err = 0
            preds = []
            max_len = fps * 30
            for spect, label in zip(spects_val, labels_val):
                # pick excerpt of 30 seconds in center of file
                excerpt = slice(max(0, (len(spect) - max_len) // 2),
                                (len(spect) + max_len) // 2)
                # crop to maximum length and required spectral bins
                spect = spect[None, excerpt, :bin_mel_max]
                # crop to maximum length and remove edges lost in the network
                label = label[excerpt][blocklen // 2:-(blocklen // 2)]
                e, pred = val_fn(spect, label)
                val_err += e
                preds.append((pred[:, 0], label))
            print("Validation loss: %.3f" % (val_err / len(filelist_val)))
            from eval import evaluate
            _, results = evaluate(*zip(*preds))
            print("Validation error: %.3f" % (1 - results['accuracy']))
            if options.save_errors:
                errors.append(val_err / len(filelist_val))
                errors.append(1 - results['accuracy'])

    # save final network
    print("Saving final model")
    np.savez(modelfile, **{'param%d' % i: p for i, p in enumerate(
            lasagne.layers.get_all_param_values(network))})
    with io.open(modelfile + '.vars', 'wb') as f:
        f.writelines('%s=%s\n' % kv for kv in cfg.items())
    if options.save_errors:
        np.savez(modelfile[:-len('.npz')] + '.err.npz',
                 np.asarray(errors).reshape(epochs, -1))

if __name__=="__main__":
    main()

