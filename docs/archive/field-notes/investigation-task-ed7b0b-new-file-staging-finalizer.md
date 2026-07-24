!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Investigation: new-file staging finalizer ground truth (task_ed7b0b)

**Task**: Establish ground truth for the dream finding about the new-file
staging finalizer tests. Read/analyze only; no source or test behavior changes.
**Parent task**: `task_3fb81f4ecd2c4881a6f4524a7a3942ad` (dream finding that
implicates the finalizer new-file staging area).
**Dependency**: `task_repair_f869c05064a7e5e12f86f872` (sibling investigation
that reached the same intact verdict).
**Repo areas audited**: `src/mac/executor_finalizer.py`
(`recover_from_new_file_refusal`, `run_deterministic_git_finalizer`,
`_split_porcelain_status`, `_new_file_finalize_message`,
`_untracked_finalize_message`, `_write_git_finalizer_refusal_manifest`),
`src/mac/executor_sandbox.py` (`finalize_with_new_file_recovery`),
`src/mac/executor_prompt.py` (`_repository_prepared_base`),
`tests/test_git_finalizer_new_file_staging.py`,
`tests/test_new_file_recovery_staging.py`,
`tests/test_new_file_recovery_wiring.py`,
`tests/test_worker_finalize_new_files.py`,
`tests/test_executor_prompt_finalizer.py`, and the finalizer cases in
`tests/test_task_executor.py`.
**Investigated by**: fleet worker (investigation only; no production code edits).

## Verdict: FALSE POSITIVE — behavior already correct and covered

The new-file staging finalizer already `git add -A`s and commits untracked and
staged-but-uncommitted new files (while respecting `.gitignore`) before the
clean/push gate, the new-file refusal recovery service is implemented AND wired,
and every named suite is green on the current tree when run hermetically. There
is no failing or missing test and no incorrect finalizer behavior behind the
finding. No source or test should be changed on the strength of it.

## Ground Truth: The Deterministic Finalizer Stages New Files First

`run_deterministic_git_finalizer` (`src/mac/executor_finalizer.py:843`) opens a
`repository_snapshot` phase, runs `git status --porcelain`, and splits it into
tracked edits, untracked paths, and staged-but-uncommitted new paths via
`_split_porcelain_status` (`src/mac/executor_finalizer.py:441`). When any bucket
is non-empty it runs `git add -A` and commits with fleet provenance
(`src/mac/executor_finalizer.py:897`) — BEFORE the `canonical_sync`, contract
test, and clean/push gates that follow. `git add -A` respects `.gitignore`, so
new source is committed while gitignored build artifacts are not.

## Ground Truth: New-File Refusal Recovery Reconstitutes The Work

`recover_from_new_file_refusal` (`src/mac/executor_finalizer.py:180`) loads the
preserved executor state, confirms via `classify_finalizer_refusal` that the
refusal really was a new-file refusal, `git add`s every preserved new file
(untracked + staged), commits with provenance, syncs with canonical, and
attempts a guarded push, returning a structured `mac.new_file_recovery.v1`
result. The `git_runner`/`push_runner` seams are injectable so
`tests/test_new_file_recovery_staging.py` exercises it without a live remote.

## Ground Truth: The Recovery Is Wired

`finalize_with_new_file_recovery` (`src/mac/executor_sandbox.py:4614`) runs the
deterministic finalizer, attempts `recover_from_new_file_refusal` (fail-closed
via `RepositoryRecoveryError` unless the refusal was new-file-only), then
re-runs the finalizer so the recovered commit yields clean, publishable
evidence. `tests/test_new_file_recovery_wiring.py` guards the wiring.

## Verification

All six named suites pass hermetically (the canonical runner unsets `MAC_*` via
`scripts/run-contract-tests.sh`; line 71 `unset "${!MAC_@}"`):

```console
$ .venv/bin/pytest -q \
    tests/test_git_finalizer_new_file_staging.py \
    tests/test_new_file_recovery_staging.py \
    tests/test_new_file_recovery_wiring.py \
    tests/test_worker_finalize_new_files.py \
    tests/test_executor_prompt_finalizer.py
28 passed
```

The over-staging guard
`test_finalizer_stages_new_source_but_never_commits_gitignored_artifacts`
(`tests/test_git_finalizer_new_file_staging.py:285`) proves intended new source
is committed, `generated/` and `*.log` gitignored artifacts are excluded, and
`git status --porcelain` is empty afterward.

## Environment Note: One Test Fails Only When The Harness Is Bypassed

Running the `test_task_executor.py` finalizer cases with the live worker
environment still present shows exactly one failure,
`test_git_finalizer_blocks_when_canonical_remote_is_missing`. This is a
test-isolation artifact, not a defect: `_repository_prepared_base`
(`src/mac/executor_prompt.py:654`) reads `MAC_TASK_REPO_BASE_SHA` from the
environment first, so the live base SHA leaks in and the finalizer's ancestry
freshness gate fires before the missing-remote gate, changing the error string.
Unsetting the `MAC_*` vars — which the canonical harness does — makes the test
pass, confirming the finalizer logic is correct and the failure is a bypassed-
harness artifact, not a new-file-staging defect.

## Disposition

No production code or test changes are warranted. The finding is a FALSE
POSITIVE; the contract prerequisite is satisfied by the existing implementation
and its regression suites. This note is the retained investigation evidence.
