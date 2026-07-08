# External activation-probe classifier contract

`ActivationProbeClassifier` is a mean-pooled logistic probe. It accepts either
`[seq_len, hidden_dim]` activations or one `[hidden_dim]` summary vector and
returns `score`, `label`, and `confidence`, all independent of the MAC hub.

Checkpoints are JSON with `weights`, `bias`, `threshold`, and optional positive
and negative labels. A missing checkpoint constructs a disabled neutral probe
(`score=0.5`, `label=disabled`, `confidence=0`). Dimension and finite-value
checks fail closed inside the classifier, while the worker integration catches
those failures because the overall audit is advisory.

This probe classifies activations supplied by its caller. It does not compute a
Jacobian lens, acquire activations from hosted models, infer hidden states from
proxy traffic, or expose the internal reasoning of a different model. It is
not an intent oracle. Its checkpoint must be trained separately from its
calibration data, versioned with the instrumented model and layer identity, and
treated as a diagnostic signal only.
