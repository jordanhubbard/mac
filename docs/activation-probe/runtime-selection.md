# External activation capture runtime

The prototype uses ordinary forward hooks on Hugging Face `transformers`
decoder blocks. This is the narrowest useful integration: it exposes
per-layer, per-token residual tensors while keeping PyTorch and model weights
out of MAC's base dependency set. `src/mac/activation_probe/runtime.py`
therefore accepts any hookable model and imports neither package itself.

Compared with TransformerLens, this covers more current open-weight model
architectures without conversion. Compared with nnsight or baukit, it adds no
runtime server or tracing dependency. The cost is that layer paths remain
model-specific and operators must validate them when registering a model.

Hooks copy only configured layers and are removed in a `finally` block. Memory
and latency overhead are proportional to selected layers, sequence length, and
hidden width; production pilots should capture one or two late residual layers
rather than every block. Model licensing remains the license of the selected
open-weight checkpoint. The mechanism is compatible with OpenShell because
all model code and weights stay inside the worker sandbox.

These hooks work only when the caller owns the forward pass and can reference
the model modules directly. They cannot be attached to a hosted-model API or
to MAC's router. Instrumenting a local observer model would capture the
observer's states only and must not be described as observing the hosted actor.
