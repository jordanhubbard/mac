# External activation-probe prototype

The prototype demonstrates optional classification of externally supplied
activation tensors without making the classifier authoritative. It provides
Hugging Face-compatible forward-hook utilities for model runtimes that an
operator owns, a small mean-pooled logistic probe, a held-out calibration
harness, and best-effort worker evidence attachment.

This is not an implementation of the Jacobian lens (J-lens). A genuine J-lens
derives layer-specific maps from one model's intermediate activations to that
same model's future output logits. This prototype computes no Jacobians and has
no access to hosted models' activations. A proxy can observe messages, tool
calls, routing, timing, and usage, but cannot recover those hidden states.

The synthetic calibration fixture produces perfect ranking and threshold
accuracy; its ECE is illustrative only and is not evidence that the probe
generalizes. A real pilot must select an open-weight checkpoint, freeze model
and layer identifiers, train the probe on a disjoint corpus, and calibrate on a
sealed dataset with recorded hashes and uncertainty intervals.

The worker feature is off by default and every emitted result declares
`advisory_only=true`. Exceptions are logged and discarded. The activation
probe has no link to task state transitions, review verdicts, publication
gates, or dispatch.

Known limitations include model-specific hook paths, activation memory cost,
distribution shift, probe gaming, and the gap between correlated activations
and causal intent. Recommended next steps are a small opt-in open-weight pilot,
checkpoint/data provenance, calibration confidence intervals, latency and
memory budgets, and blinded human evaluation. Approval authority must remain
with externally validated evidence and independent review. No operational
value is claimed until a controlled, instrumented model runtime and a
scientifically valid dataset demonstrate out-of-distribution predictive value.
