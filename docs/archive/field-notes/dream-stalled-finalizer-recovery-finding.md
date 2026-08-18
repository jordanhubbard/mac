!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Dream-repair review finding: stalled-finalizer recovery

This note records the investigation outcome for a low-confidence dream-cycle
repair finding (fingerprint `dreamrepair:9a65379bca2038941c22a69eca8814c2`,
scope `project`, repo area `mac`). It is the durable, product-tracked artifact
for that review; the per-run executor/worker diagnostics live in the task
workspace and are intentionally kept out of git.

## Scope

The finding is a `failure_pattern` supported by a single memory record
(`mem_a52c2968410947e5baad3b3974c78845`,
task `task_repair_e57870febced381b628b6a68`) whose candidate summary is:

> Repair contract prerequisites: Implement hub-side stalled-finalizer recovery
> in `src/mac/repository_recovery.py` (inspect + recover).

This review examined whether that finding maps to an actionable defect or a
missing deliverable in the repository-recovery code path.

## Finding

The finding is **not actionable as a code change**: the deliverable it points at
is already implemented, wired, documented, and tested in the current tree.

- `src/mac/repository_recovery.py` provides both halves of the stalled-finalizer
  path: `inspect_stalled_finalizer_recovery` returns a mutation-free recovery
  plan for a finalizer interrupted by timeout, cancellation, or crash (a
  `finalizer-progress.json` stuck in a non-terminal status), and
  `recover_stalled_finalizer` harvests verified work, revalidates, and performs
  the guarded push.
- The path is CLI-wired via `task recover-stalled-finalizer`
  (`cmd_task_recover_stalled_finalizer` in `src/mac/cli.py`), including
  `--approve-new-file`, `--evidence-id`, and `--execute` (read-only plan by
  default), and is listed in `docs/reference/cli.md`.
- Coverage exists in `tests/test_repository_recovery.py` exercising both the
  inspection plan and the recovery flow; the suite passes cleanly.

The single supporting record is a `[failure]` on the *original* implementation
task's contract prerequisites, not evidence of a defect in the shipped
recovery code. With only one low-confidence evidence record and no reproducible
fault in the current code, there is no defect to repair.

## Disposition

- Classification: low-confidence dream finding, **not actionable** — deliverable
  already present.
- Code change: none in `src/mac/repository_recovery.py` or its CLI wiring.
- Evidence gap: the finding lacks any record isolating a reproducible fault in
  the shipped `inspect_stalled_finalizer_recovery` /
  `recover_stalled_finalizer` path; its lone record is the original repair
  task's prerequisite failure. Reopen only if a concrete reproduction or a
  new fingerprint recurrence surfaces an actual defect.

## References

- `src/mac/repository_recovery.py` — `inspect_stalled_finalizer_recovery` and
  `recover_stalled_finalizer` implementations.
- `src/mac/cli.py` — `task recover-stalled-finalizer` command wiring.
- `tests/test_repository_recovery.py` — inspection and recovery coverage.
- `docs/archive/field-notes/crash-incident-finding.md` — companion investigation-finding note for
  an unactionable low-signal repair candidate.
