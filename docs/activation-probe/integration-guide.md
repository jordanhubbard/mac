# External activation-probe worker integration

The external activation probe is disabled by default. Set
`MAC_ACTIVATION_PROBE_ENABLED=1` and
`MAC_ACTIVATION_PROBE_CHECKPOINT=/path/to/checkpoint.json` to enable it.
Activations must come from a model runtime that the operator owns and has
explicitly instrumented. They may be provided by executor metadata as
`activation_probe_activations` or through
`MAC_ACTIVATION_PROBE_ACTIVATIONS_FILE`; the default file is
`<task-workspace>/activation-probe-activations.json`.
If the feature is enabled without a checkpoint, the worker emits a bounded
neutral `disabled` result and does not attempt to read activations.

When successful, the worker adds `metadata.activation_probe_audit` to task
evidence with `score`, `label`, `confidence`, `runtime_ms`, `schema`, and
`advisory_only=true`. Missing input, bad checkpoints, dimension mismatches, and
all other audit errors emit a warning and do not change executor return codes,
verification, review, or publication. No code path reads the audit to approve,
reject, claim, dispatch, or complete a task.

This integration cannot observe activations from Codex, Claude, or any other
opaque hosted-model API. Routing or intercepting their requests and responses
reveals only traffic content and metadata, not their intermediate state. A
separate local observer model would expose only that observer's activations,
not the hosted actor model's activations.
