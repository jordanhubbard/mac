# Ground-Truth Audit: Bounded/Batched Repository-Ref Audit Hub Lookups

Read-only audit for the contract-repair of parent `task_repair_798203af9af68a97a4d843c0`
(parent title: "Bound and batch repository-ref audit hub lookups"; contract `.mac/project.yaml`).
No source, test, skill, or deploy files were modified; no code commits were made. This document
records ground truth only.

## Summary Verdict

The bounded/batched repository-ref audit hub-lookup deliverable is **already present, tracked,
and passing in HEAD** (`ec3f720c25037705f5f7a784779cde4fb89f574c`). No source/commit/push work
remains for the bounding/batching deliverable itself. The scoped contract gate passes cleanly
(77 tests, exit 0) and the worktree is clean with all four target files tracked at the baseline
commit.

## 1. Bootstrap

Command: `python3 scripts/bootstrap-project.py`

- Exit code: `0`
- Provisioned outputs present:
  - `.venv/bin/python` (symlink -> `python3.12`)
  - `.venv/bin/pytest`
  - `.venv/bin/coverage`

## 2. Parent Deliverable Present in HEAD

`src/mac/repository_hygiene.py`:
- `class _BoundedTaskLoader` defined at line 128.
- Caps a single lookup via per-task timeout: `_per_task_timeout` + `_lookup_timeout()` combine
  the per-task budget with the remaining overall deadline (`min(...)`), and `future.result(timeout=...)`
  enforces it per lookup.
- Caps total wall time via overall deadline: `deadline`/`_remaining()` derived from
  `audit_deadline_seconds`.
- Bounds concurrency: `ThreadPoolExecutor(max_workers=self._concurrency)` with
  `self._concurrency = max(1, int(concurrency))`.
- Records timed-out/unavailable task IDs without blocking: `timed_out`/`_record_timeout(...)`
  and cached `None` sentinel for unavailable/errored tasks; the caller is never blocked
  (timeouts, exceptions, and post-deadline lookups all resolve to a safe `None`).
- `def audit_repository_refs_result(...)` at line 849 exposes `audit_deadline_seconds`,
  `per_task_timeout_seconds`, and `lookup_concurrency`, and dedupes/preloads/batches
  `task_detail` hub lookups via `bounded.preload(...)` (primary refs at line 906,
  replacement IDs at line 920).

`src/mac/repository_ref_reconciler.py`:
- Imports `audit_repository_refs_result` from `mac.repository_hygiene` (line 21).
- Drives the audit per repository via `audit_repository_refs_result(...)` (line 413).

## 3. Regression Coverage

`tests/test_repository_hygiene.py` — "Bounded / batched audit regression coverage" section
(begins line 836) exercises the bounded surface:
- `test_slow_hub_stays_within_deadline_and_reports_timeout`
- `test_one_stalled_task_does_not_block_other_refs`
- `test_partial_result_structure_is_complete_and_does_not_hang`
- `test_task_detail_lookups_are_deduped_and_cached` (primary/replacement batching, at-most-one
  loader call per task_id)
- `test_fail_closed_when_primary_or_replacement_detail_unavailable`
- `test_keyboard_interrupt_yields_clean_partial_report`

`tests/test_repository_ref_reconciler.py` — imports `audit_repository_refs_result` from
`mac.repository_hygiene` and imports `mac.repository_ref_reconciler`; audit-mode tests
(`test_audit_mode_never_executes_prune`, `test_prune_requires_canonical_remote_but_audit_does_not`,
`test_pull_request_failure_warns_in_audit_and_blocks_prune`,
`test_failed_or_abandoned_attempt_without_observed_head_is_explicit_audit_debt`) exercise the
reconciler's use of the surface.

## 4. Canonical Contract Gate (Scoped)

Command:
`scripts/run-contract-tests.sh tests/test_repository_hygiene.py tests/test_repository_ref_reconciler.py -q`

- Result: `77 passed`
- Exit code: `0`

## 5. Git Ground Truth

- `git status --porcelain`: clean (empty output before findings.md was added).
- HEAD: `ec3f720c25037705f5f7a784779cde4fb89f574c`
- `git ls-files --error-unmatch` (exit 0) confirms tracked:
  - `src/mac/repository_hygiene.py`
  - `src/mac/repository_ref_reconciler.py`
  - `tests/test_repository_hygiene.py`
  - `tests/test_repository_ref_reconciler.py`
- All four files present in the baseline commit (`git cat-file -e HEAD:<path>` succeeds for each).

## Conclusion

No source/commit/push work remains for the bounding/batching deliverable. The bounded loader
(`_BoundedTaskLoader`) enforces per-task timeout, overall deadline, and bounded concurrency while
recording timed-out/unavailable task IDs without blocking, and `audit_repository_refs_result`
dedupes/preloads/batches hub lookups, driven by the reconciler with matching regression tests.
The deliverable is complete and green in HEAD.
