import theano
import theano.tensor as T
import numpy as np
import time

from metrics import multi_label_sample_accuracy

class SGD:
    """ Implementation of stochastic gradient descent.
    """
    def __init__(self, batch_size, init_rate, nb_epochs, learning_schedule='fixed',
                 update_rule='simple', accuracy_measure='sample', verbose=0):
        """ Initialized the optimization method.
        
        Arguments:
            batch_size
                approximate number of samples for each mini-batch. The actual
                number will vary slightly to divide the dataset cleanly.
            init_rate
                initial learning rate. Will be updated according to the
                update rule.
            nb_epochs
                number of epochs, or number of iterations over the entire
                training set, to run before stopping.
            learning_schedule
                rule to update the learning rate. Can be:
                - 'constant' for constant learning rate fixed to the initial rate.
                - ('decay', decay_factor) for 
                  learning rate decaying when validation error does not decrease 
                  after an epoch.
            update_rule
                rule to update the parameters. Can be:
                - 'simple' for simply w(t) = w(t-1) - alpha * grad(t)
                - ('momentum', mtm) for the momentum method:
                  w(t) = w(t-1) - alpha * dw(t)
                  dw(t) = mtm * dw(t-1) + grad(t)
                - ('rprop', inc_rate, dec_rate) for the rprop method,
                  should be used with large mini-batches only.
            accuracy_measure
                measure of accuracy to use on the validation set:
                - 'sample' for the regular, number of samples gotten right
                  measure.
            verbose
                verbosity level: 0 for no messages, 1 for messages every epoch, 2
                for messages every iteration.
        """
        self.batch_size = batch_size
        self.init_rate = init_rate
        self.nb_epochs = nb_epochs
        self.learning_schedule = learning_schedule
        self.update_rule = update_rule
        self.accuracy_measure = accuracy_measure
        self.verbose = verbose

    def optimize(self, model, samples, valid_data, compile_mode=None):
        # Determine batches by picking the number of batch which,
        # when used to divide the number of samples, best approximates
        # the desired batch size.
        flt_nb_samples = float(len(samples))
        ideal_nb_batches = flt_nb_samples / self.batch_size
        lower_nb_batches = np.floor(ideal_nb_batches)
        upper_nb_batches = np.ceil(ideal_nb_batches)
        lower_error = abs(flt_nb_samples / lower_nb_batches - self.batch_size)
        upper_error = abs(flt_nb_samples / upper_nb_batches - self.batch_size)
        nb_batches = (int(lower_nb_batches) if lower_error < upper_error 
                      else int(upper_nb_batches))
        # Split the dataset into that number of batches in roughly equal-sized
        # batches.
        splits = np.round(np.linspace(
            0, len(samples), 
            num=nb_batches+1)
        ).astype(np.int32)
        
        # Store mini-batches into a shared variable. Since theano shared variables
        # must have constant storage space, we'll initialize to the shape of the
        # largest batch.
        largest_batch_size = 0
        for i in range(nb_batches):
            batch_size = splits[i+1] - splits[i]
            largest_batch_size = max(batch_size, largest_batch_size)
        batch = theano.shared(
            np.empty(
                [largest_batch_size] + samples.sample_shape,
                theano.config.floatX
            ),
            name='batch'
        )
        # Similarly for the batch labels.
        batch_labels = theano.shared(
            np.empty(
                [largest_batch_size],
                np.int32
            ),
            name='batch_labels'
        )

        # Compile the theano function to run a full SGD iteration.
        cost = model.cost_function(batch, batch_labels)
        updates = []
        
        learning_rate = theano.shared(
            np.float32(self.init_rate)
        )

        parameters = model.parameters()

        # Update rule.
        if self.update_rule == 'simple':
            for param in parameters:
                updates.append(
                    (param, param - learning_rate * T.grad(cost, param))
                )
        elif self.update_rule[0] == 'momentum':
            mtm = self.update_rule[1]
            # Keep track of the update at t-1.
            prev_updates = []
            for param in parameters:
                param_shape = param.get_value().shape
                prev_updates.append(theano.shared(
                    np.zeros(param_shape, theano.config.floatX)
                ))
            # And update the weights by taking into account a momentum
            # from t-1.
            for i in range(len(parameters)):
                cur_update = (
                    mtm * prev_updates[i] + learning_rate * 
                    T.grad(cost, parameters[i])
                )
                updates += [
                    (parameters[i], parameters[i] - cur_update),
                    (prev_updates[i], cur_update)
                ]
        else:
            raise ValueError("Invalid update rule!")

        run_iteration = theano.function(
            [],
            [cost],
            updates=updates,
            mode=compile_mode
        )
        predict_label = None
        prev_acc = None
        prev_dec = 0

        # Run the actual iterations, shuffling the dataset at each epoch.
        for t in range(1, self.nb_epochs + 1):
            permutation = np.random.permutation(len(samples))
            samples.shuffle(permutation)
            samples_iterator = iter(samples)
            train_labels = samples.get_labels()
            avg_cost = np.array([0], theano.config.floatX)
            
            for i in range(nb_batches):
                if self.verbose == 2:
                    print "Preparing batch " + repr(i+1) + " out of " + repr(nb_batches)
                prepare_start = time.clock()
                # Select the batch.
                batch_size = splits[i+1] - splits[i]
                new_batch = np.empty(
                    [batch_size] + samples.sample_shape,
                    theano.config.floatX
                )
                
                for j in range(batch_size):
                    new_batch[j] = samples_iterator.next()
                prepare_end = time.clock()
                if self.verbose == 2:
                    print "Prepared the batch in " + repr(prepare_end - prepare_start) + " seconds."
                # Run the iteration.
                iter_start = time.clock()
                batch.set_value(new_batch)
                batch_labels.set_value(train_labels[splits[i]:splits[i+1]])
                cost_val = run_iteration()
                iter_end = time.clock()
                avg_cost += cost_val
                if self.verbose == 2:
                    print "Batch " + repr(i+1) + " out of " + repr(nb_batches)
                    print "Cost running average: " + repr(avg_cost / (i+1))
                    print "Processed in " + repr(iter_end - iter_start) + " seconds."
            # Learning schedule.
            if self.learning_schedule == 'constant':
                pass
            elif self.learning_schedule[0] == 'decay':
                # Compute a validation error rate, decay the learning rate
                # if it didn't decrease since last epoch.
                decay, delay = self.learning_schedule[1:]
                # If the validation error rate function wasn't compiled yet,
                # do it. We assume the validation set fits into VRAM for GPU
                # implementations, which might be a tad unrealistic. In the
                # future, it would be better to split it into mini-batches and
                # accumulating the results to maximise the amount of memory to
                # dedicate to model parameters - ideally, reusing the batch
                # shared variable.
                if predict_label == None:
                    vs = T.tensor4('valid_samples')
                    predict_label = theano.function(
                        [vs],
                        T.argmax(model.forward_pass(vs), axis=1),
                        mode=compile_mode
                    )

                valid_samples = np.empty(
                    [len(valid_data)] + valid_data.sample_shape,
                    theano.config.floatX
                )
                tofrozenset = lambda l: l if isinstance(l, frozenset) else frozenset([l])
                valid_labels = map(tofrozenset, valid_data.get_labels())
                i = 0
                for sample in valid_data:
                    valid_samples[i] = sample
                    i += 1
                predicted_labels = map(
                    lambda i: set([i]),
                    list(predict_label(valid_samples))
                )
                current_acc = multi_label_sample_accuracy(valid_labels, predicted_labels)

                if current_acc != None and prev_acc >= current_acc:
                    if prev_dec == delay:
                        if self.verbose >= 1:
                            print "Validation accuracy not increasing, decaying."
                        learning_rate.set_value(
                            np.float32(learning_rate.get_value() * decay)
                        )
                        prev_dec == 0
                    else:
                        prev_dec += 1
                else:
                    prev_dec == 0
                prev_acc = current_acc
            else:
                raise ValueError(repr(self.learning_schedule) 
                                 + " is not a valid learning schedule!")
            
            if self.verbose >= 1:
                print "Epoch " + repr(t)
                print "Cost: " + repr(avg_cost / nb_batches)
                if prev_acc != None:
                    print "Validation accuracy: " + repr(prev_acc)
