# J-lens runtime selection

The prototype uses ordinary forward hooks on Hugging Face `transformers`
decoder blocks. This is the narrowest useful integration: it exposes
per-layer, per-token residual tensors while keeping PyTorch and model weights
out of MAC's base dependency set. `src/mac/jlens/runtime.py` therefore accepts
any hookable model and imports neither package itself.

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
