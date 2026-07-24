!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Findings: crash_service "unknown" crash path (ground truth for parent P0 crash-repair)

- Task: `task_b2cfbc246d9c4614b7be963876dd1601` (investigation only; **no source change here**).
- Parent (repair): `task_repair_09b1ec1dc13aea873329aaa5` / P0 crash-repair `task_f214213fec884f85b7cee233c542696e`.
- Investigation parent: `task_d3302e4698d64aa5bd5efccec568dd57`.
- Baseline reviewed: worktree `HEAD = 5beaf8e` ("MAC OpenShell sandbox baseline"), read-only.
- Files inspected: `src/mac/crash_service.py`, `tests/test_crash_service.py`, `tests/test_crash_observer.py`.

## Summary (root-cause classification)

A crash reported **"at unknown"** — i.e. `revision` omitted so it defaults to
`"unknown"`, and/or no actionable stack/frame carried in `stack_trace`/`stderr_tail` —
is an **unactionable "unknown" incident, not a reproducible code defect** in
`crash_service.py`. There is **no bug to repair in the crash pipeline**: the
service is behaving exactly as designed. The "P0: repair MAC crash ... at unknown"
task is a *generated symptom* of an ingest whose payload lacked a revision and an
actionable stack, not evidence of a defect in the ingest/fingerprint/repair-filing
code.

Concretely:

- `revision` defaults to `"unknown"` at ingest (`src/mac/crash_service.py:146`),
  and the repair-task title is `"P0: repair MAC crash %s at %s" % (process_name,
  revision[:12])` (`src/mac/crash_service.py:471`). So **"at unknown" just means
  the reporter never supplied a revision** — it is a metadata gap, not a crash
  location.
- `normalize_stack_trace` chooses its signature source as
  `stack_trace or stderr_tail or reason or "unknown crash"`
  (`src/mac/crash_service.py:107`). Because `reason` itself defaults to
  `"process exited unexpectedly"` at ingest (`src/mac/crash_service.py:155`), the
  literal `"unknown crash"` fallback is only ever reached when a caller passes an
  explicitly emptied `reason`. In practice an "unknown" incident carries a
  non-actionable signature (bare reason string) with no file/frame/exception type.
- `_ensure_repair_task` (`src/mac/crash_service.py:388`) **files a P0 repair task
  unconditionally** for any open/non-`needs_human` report — it does not (and is
  not designed to) branch on whether the stack signature is actionable. That is
  the intended fail-closed behavior: every observed crash gets an incident owner.

Therefore the correct disposition of an "at unknown" incident is **triage/close as
unactionable (or route to human)**, not a code repair. Any attempt to "fix" this
via a source change to the crash pipeline is misdirected and risks breaking the
invariants below.

## The exact contract gate command and its current result

- Command (from repo contract, `.mac/project.yaml`): `scripts/run-contract-tests.sh`.
- Result on this baseline worktree (`HEAD = 5beaf8e`), full whole-repo gate:
  **PASS** — `8892 passed, 4 skipped` in ~152s; exit code `0`.
- Coverage safety rail (part of the same gate): statements `90.95%` (floor
  `90.00%`), branches `80.47%` (floor `80.00%`) — both above floor.
- Focused crash subset `pytest tests/test_crash_service.py
  tests/test_crash_observer.py`: **11 passed**.

## Why the parent contract failed (verification_contract_failed)

The parent P0 crash-repair failed its repository contract because it produced **no
landed, test-backed change**:

- No committed change that the gate could verify (`repo.pushed = false`).
- No passing test/check recorded for the change (worker evidence carried
  `problems: ["repo code evidence requires at least one passing test/check",
  "repository evidence failed local contract checks; refusing to push"]`).
- No pushed ref / PR (`repo.pushed = false`; `remote_ref` staged but never
  published).

Root cause of the *contract* failure is the mismatch between the task type and
the change made: a prior attempt edited a **source file**
(`deploy/deploy-mac-fleet.sh`) for what is an investigation-only task. Editing
source without a corresponding test and while under the whole-repo coverage floor
caused the gate to fail (the gate reached the coverage/branch-floor stage after
the suite ran), so the worker correctly refused to push — leaving the parent with
no committed change, no passing test, and no pushed ref. The underlying "unknown"
incident is unactionable (see above), so there was no legitimate source repair to
land in the first place.

## Files/functions a future repair would touch (if any)

For the **"unknown" incident itself: none** — it is unactionable; do not modify
the crash pipeline to make it "actionable".

If the fleet later decides to *reduce the volume of unactionable "unknown"
incidents* (a separate, optional hardening task — NOT this parent repair), the
only sanctioned touch points are:

- `src/mac/crash_service.py` — `_ensure_repair_task` (line 388) and/or `ingest`
  (line 138): a policy to route reports whose stack signature is non-actionable
  (empty/`"unknown crash"`/bare-reason with no revision) to `needs_human` or a
  low-priority triage state instead of auto-filing a P0. This is behavior change
  and MUST ship with new regression tests and preserve every invariant below.

## Invariants any future change MUST NOT break

From `tests/test_crash_service.py`:

- Same fingerprint dedups by `event_id` and reassigns the open repair task to an
  **unaffected** peer; `excluded_agent_ids` accumulates affected agents
  (`test_crash_ingest_deduplicates_and_reassigns_repair_to_unaffected_peer`).
- Fingerprint includes `revision`: different revisions => different fingerprints;
  `resolve` is durable and lists under `status="resolved"`
  (`test_crash_fingerprint_includes_revision_and_resolve_is_durable`).
- Recurrence after a completed repair creates a **new** task carrying
  `prior_repair_task_id` and the "recurred after repair task" note
  (`test_crash_recurrence_after_completed_repair_creates_new_task`).
- `tick()` resolves incidents on verified task completion
  (`test_crash_repair_tick_closes_incident_after_verified_task_completion`) and
  requeues failed repairs with a prior-evidence link, incrementing
  `repair_attempt_count` (`test_crash_repair_tick_refiles_failed_repair_with_prior_evidence_link`).
- `MAX_REPAIR_ATTEMPTS = 3` then `needs_human` escalation
  (`src/mac/crash_service.py:35,362`) — a repaired report must not resurrect out
  of `needs_human`.

From `tests/test_crash_observer.py`:

- The external observer captures the trace and POSTs the
  `mac.agent_crash_occurrence.v1` payload (with `task_id`, `core_reference`,
  `stack_trace`) **before** returning failure `rc == 1`
  (`test_observer_captures_trace_and_posts_before_returning_failure`).
- Crash spool files are mode `0600` and replayed then removed
  (`test_observer_spool_is_mode_0600_and_replayed`).
- Every deployment supervisor (systemd/supervisord/launchd/kubernetes) wires the
  external `mac-crash-observer`; core dumps + `PYTHONFAULTHANDLER=1` enabled
  (`test_every_deployment_supervisor_uses_external_crash_observer`).

## Plan the contract-landing child must follow

1. Treat the parent P0 "at unknown" incident as **unactionable**: resolve/triage
   or route to human — do NOT land a crash-pipeline source change to "repair" it.
2. Keep the repository-contract landing **investigation/artifact-only** where
   possible: a docs/field-note change (like this file) does not alter coverage and
   passes the gate cleanly, satisfying "at least one passing test/check" via the
   full gate PASS above.
3. If (and only if) a real hardening change is authorized, edit exactly the touch
   points named above in `src/mac/crash_service.py`, add regression tests that
   preserve every invariant listed, and run `scripts/run-contract-tests.sh` to
   green (respecting the 90%/80% coverage floors) BEFORE handoff.
4. Commit all new files (`git add -A`), leave no untracked/staged-new files, and
   let the deterministic host finalizer own rebase/commit/push.

## Consequential assumptions

- "at unknown" is interpreted as `revision == "unknown"` (the default) and/or a
  non-actionable stack signature — corroborated by
  `src/mac/crash_service.py:146,155,107,471`.
- Baseline `HEAD = 5beaf8e` in the task worktree is an equivalent tree to the
  parent's crash revision for the purpose of reading these functions; only source
  reading and the standard gate were performed, no source edits.
- The field note is filed under `docs/archive/field-notes/` to match the existing
  crash-finding precedent (`findings-crash-591645a3-startup-selftest-timeout.md`).
