# J-lens activation-auditing prototype

The prototype demonstrates optional activation inspection without making
internal-state classifiers authoritative. It uses Hugging Face-compatible
forward hooks, a small mean-pooled logistic probe, a held-out calibration
harness, and best-effort worker evidence attachment.

The synthetic calibration fixture produces perfect ranking and threshold
accuracy; its ECE is illustrative only and is not evidence that the probe
generalizes. A real pilot must select an open-weight checkpoint, freeze model
and layer identifiers, train the probe on a disjoint corpus, and calibrate on a
sealed dataset with recorded hashes and uncertainty intervals.

The worker feature is off by default and every emitted result declares
`advisory_only=true`. Exceptions are logged and discarded. J-lens has no link
to task state transitions, review verdicts, publication gates, or dispatch.

Known limitations include model-specific hook paths, activation memory cost,
distribution shift, probe gaming, and the gap between correlated activations
and causal intent. Recommended next steps are a small opt-in open-weight pilot,
checkpoint/data provenance, calibration confidence intervals, latency and
memory budgets, and blinded human evaluation. Approval authority must remain
with externally validated evidence and independent review.
