!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Crash incident investigation finding

This note records the investigation outcome for the P0 crash-repair incident
that opened against the agent crash-reporting subsystem
(`src/mac/crash_service.py`). It is the durable, product-tracked artifact for
that investigation; the per-run executor/worker diagnostics live in the task
workspace and are intentionally kept out of git (see
`src/mac/investigation_artifacts.py`).

## Scope

The crash observer records an unexpected worker exit, the hub redacts the
payload and computes a deduplication fingerprint from the source revision, the
process name, the termination class, and a normalized stack signature. The
first occurrence of a new fingerprint opens one immediately dispatchable P0
`mac` repair task. This investigation examined whether that reported occurrence
maps to an actionable defect in the crash-reporting/repair code path.

## Finding

The reported occurrence is an **unactionable "unknown" incident**. The
observed evidence does not isolate a reproducible defect in
`src/mac/crash_service.py` or the surrounding repair-dispatch path:

- The occurrence carries no fatal Python or native stack that resolves to a
  crash-service code frame. With an empty trace the hub falls back to the
  `"unknown crash"` stack signature and an `"unknown"` revision, exactly the
  degenerate-input path the ingest code already handles deterministically.
- Fingerprinting, occurrence persistence, dedup by `event_id`, affected-agent
  accounting, and the `needs_human` escalation after the attempt ceiling all
  behave as specified when re-read against the current code; none exhibits a
  correctness fault attributable to this occurrence.
- No supervisor-, revision-, or node-specific signal ties the exit to a
  MAC-owned regression rather than an external/host-level termination.

Because the incident does not identify an actionable defect, this finding does
**not** modify `src/mac/crash_service.py`. Per the crash repair policy, an
incident that cannot be reduced to a reproducible fix stays an `unknown`
occurrence; if the same fingerprint recurs it accrues additional occurrences
and, after the autonomous attempt ceiling, escalates to `needs_human` with an
operator notification. That escalation path is the correct disposition for an
unknown incident and requires no code change here.

## Disposition

- Classification: `unknown` (unactionable) crash incident.
- Code change: none in the crash-reporting path.
- Follow-up: reopen with a concrete, MAC-owned stack signature or a
  reproduction if the fingerprint recurs; otherwise the existing
  occurrence-count and `needs_human` escalation handle recurrence.

## References

- `docs/crash-diagnosis-and-repair.md` — crash observer, evidence lifecycle,
  hub API, and repair policy.
- `src/mac/crash_service.py` — ingest, fingerprinting, and repair dispatch.
- `src/mac/investigation_artifacts.py` — why per-run investigation diagnostics
  are not committed to the tree.
