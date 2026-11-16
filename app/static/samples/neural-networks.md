# Neural Networks: A Working Study Guide

## 1. What a neural network actually is

A neural network is a chain of simple mathematical functions stacked in layers. Each layer takes a vector of numbers, multiplies it by a matrix of weights, adds a bias vector, and passes the result through a non-linear activation function. Nothing more mysterious than that happens inside a feed-forward network. The word "neural" is a metaphor borrowed from biology; the units are not neurons and the network does not think.

The power of the arrangement comes from composition. A single linear layer can only represent a straight-line relationship. Stack two layers with a non-linearity between them and the network can approximate curved boundaries. The universal approximation theorem states that a network with a single sufficiently wide hidden layer can approximate any continuous function on a bounded region to arbitrary accuracy. This theorem is often quoted more strongly than it deserves: it says such a network exists, not that gradient descent will find it, and not that the network will generalise to unseen data.

## 2. The forward pass

The forward pass is the computation that turns an input into a prediction. In a two-layer network the sequence is:

1. Compute the hidden pre-activation z1 = W1 x + b1.
2. Apply the activation, h = relu(z1).
3. Compute the output logits z2 = W2 h + b2.
4. Convert logits to probabilities with the softmax function.

Every quantity in this sequence is a tensor, and every operation is differentiable. Differentiability is the entire design constraint: if a step cannot be differentiated, gradient descent cannot assign credit to the parameters that came before it.

## 3. Loss, gradients and backpropagation

A loss function measures how wrong a prediction is. For classification the standard choice is cross-entropy loss, which is the negative log probability assigned to the correct class. Cross-entropy punishes confident mistakes far more heavily than hesitant ones, which is exactly the behaviour we want during training.

Backpropagation is the chain rule applied to the computational graph, run from the output back toward the input. At each node the local derivative is multiplied by the derivative flowing in from above. The result is the gradient of the loss with respect to every weight and bias, at a cost roughly equal to one forward pass. This efficiency, not any property of the brain, is why backpropagation became the default training algorithm.

Three failure modes deserve attention. Vanishing gradients occur when repeated multiplication by small derivatives shrinks the signal to nothing, so early layers learn almost nothing. Exploding gradients do the opposite and produce enormous updates that destroy the weights. Saturation happens in sigmoid and tanh units when their input is far from zero, where the derivative is nearly flat.

## 4. Optimisation in practice

Stochastic gradient descent updates each parameter in the direction that reduces the loss, using a mini-batch of examples rather than the full dataset. The mini-batch introduces noise, and that noise is useful: it helps the optimiser escape sharp, narrow minima that generalise poorly.

Momentum accumulates a running average of past gradients so the update keeps moving through flat regions and small oscillations cancel out. Adam combines momentum with a per-parameter scaling of the step size based on the second moment of the gradients, which makes it robust to badly scaled features and largely removes the need to hand-tune the learning rate per layer. AdamW adds decoupled weight decay, and it is the usual default for transformer training.

The learning rate is the single most important hyperparameter. Too large and the loss diverges or oscillates; too small and training appears to stall. A warm-up period, in which the rate rises linearly over the first few hundred steps, stabilises the beginning of training when the gradients are least reliable. Cosine decay afterward usually improves final quality.

## 5. Overfitting and regularisation

Overfitting means fitting the noise in the training set rather than the underlying pattern. The symptom is a training loss that keeps falling while validation loss turns upward. The gap between the two curves is the honest measure of the problem.

Weight decay penalises large weights, gently pushing the model toward simpler functions. Dropout randomly zeroes a fraction of activations during training, which prevents units from co-adapting and acts like an ensemble of many smaller networks. Early stopping simply halts training at the point of best validation performance. Data augmentation, where inputs are transformed in label-preserving ways, is often the strongest regulariser available and costs almost nothing.

## 6. Evaluating a model honestly

Accuracy is a poor summary when classes are imbalanced. A fraud detector that labels every transaction legitimate can be 99.5 percent accurate and completely useless. Precision answers the question "of the cases I flagged, how many were real?" Recall answers "of the real cases, how many did I catch?" The F1 score is their harmonic mean. A confusion matrix shows all four outcomes at once.

Always hold out a test set that is never touched during development, and never tune hyperparameters against it. If the test set is consulted repeatedly, it stops being a test set: the model has been selected using information from it, and the reported number is optimistic.

## 7. Where the field is now

Attention replaced recurrence as the dominant sequence operation because it can be computed in parallel across positions. A transformer block alternates self-attention, which lets every position look at every other position, with a position-wise feed-forward network. Layer normalisation and residual connections keep deep stacks trainable.

Scale brought qualitatively new behaviour. With more parameters, more data and more compute, models began to perform tasks they were never explicitly trained on, and the loss curve followed a predictable power law over many orders of magnitude. Emergent capability, in-context learning and instruction following are all consequences of this scaling, and none of them were designed in.

The open problems are not small. Models still hallucinate confident falsehoods, they remain expensive to serve, and their internal computation is largely opaque. Mechanistic interpretability, retrieval augmentation and careful evaluation are the three most active responses. A student entering the field should be sceptical of benchmark headlines and comfortable reading a loss curve.

## 8. How to study this material

Work through the forward pass with real numbers on paper: pick a two-input, one-hidden-unit network and compute the output for a specific input vector. Then compute one gradient by hand. Nothing in deep learning becomes intuitive until you have pushed a number through the chain rule yourself.

Next, reproduce a small result. Train a tiny network on a dataset you can plot, deliberately overfit it, then add regularisation and watch the validation curve change. The lesson is in the curve, not in the code. Finish by writing, in five sentences, why backpropagation is cheap and why that matters. If you can do that without notes, you understand the core of the subject.
