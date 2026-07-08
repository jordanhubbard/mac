# Advisory worker integration

J-lens is disabled by default. Set `MAC_JLENS_ENABLED=1` and
`MAC_JLENS_CHECKPOINT=/path/to/checkpoint.json` to enable it. Activations may be
provided by executor metadata as `jlens_activations` or through
`MAC_JLENS_ACTIVATIONS_FILE`; the default file is
`<task-workspace>/jlens-activations.json`.
If the feature is enabled without a checkpoint, the worker emits a bounded
neutral `disabled` result and does not attempt to read activations.

When successful, the worker adds `metadata.jlens_audit` to task evidence with
`score`, `label`, `confidence`, `runtime_ms`, `schema`, and
`advisory_only=true`. Missing input, bad checkpoints, dimension mismatches, and
all other audit errors emit a warning and do not change executor return codes,
verification, review, or publication. No code path reads the audit to approve,
reject, claim, dispatch, or complete a task.
