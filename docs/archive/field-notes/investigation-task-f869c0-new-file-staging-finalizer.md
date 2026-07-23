!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Investigation: new-file staging finalizer ground truth (task_f869c0)

**Task**: Confirm ground truth for the new-file staging finalizer suites so a
contract-prerequisite scoped to that subsystem can be assessed. Investigation
only; no behavior changes.
**Parent task**: `task_ed7b0bb064a74a4e97750b10681174eb`
(contract-prerequisite for the finalizer new-file staging area).
**Repo areas mapped**: `src/mac/worker.py`, `src/mac/executor_finalizer.py`,
`src/mac/executor_prompt.py`, `src/mac/repository_recovery.py`,
`tests/test_worker_finalize_new_files.py`,
`tests/test_git_finalizer_new_file_staging.py`,
`tests/test_new_file_recovery_staging.py`,
`tests/test_executor_prompt_finalizer.py`.
**Investigated by**: fleet worker (investigation only; no production code edits).

## Status: PREREQUISITE IS INTACT — no reproducible defect

The new-file staging finalizer path is wired correctly and its regression
suites are green on the current tree. The success-path host finalizer stages
untracked and staged-but-uncommitted new files before the cleanliness gate, the
new-file refusal recovery service reconstitutes preserved work into a
publishable commit, and the executor prompt surfaces the exact leftover new
files so the next agent commits them up front. There is no reproducible defect
behind a finding that implicates this subsystem, so no source or test should be
changed on the strength of it.

## Ground Truth: The Success-Path Finalizer Stages New Files First

The host finalizer commits the complete synchronized worktree change rather
than trusting the sandbox Git index:

- `src/mac/worker.py:4245` (`_commit_dirty_repository_worktree`) inspects
  `git status --porcelain`, then splits it into tracked edits, untracked
  paths, and staged-but-uncommitted new paths via
  `_split_repository_porcelain_status` (`src/mac/worker.py:7100`).
- When any of those three buckets is non-empty it runs `git add -A`
  (`src/mac/worker.py:4266`) so newly created modules follow the same
  test/CodeGraph/push contract as edits — the fix for the "repository worktree
  has uncommitted changes" attempt waste
  (`tests/test_worker_finalize_new_files.py:1`).

## Ground Truth: New-File Refusal Recovery Reconstitutes The Work

A refusal that fired solely because new files were left uncommitted preserves
the verified worktree instead of dropping the agent's work:

- `src/mac/executor_finalizer.py:180` (`recover_from_new_file_refusal`) loads
  the preserved executor state, confirms the refusal really was a new-file
  refusal via `classify_finalizer_refusal`
  (`src/mac/executor_finalizer.py:232`), `git add`s every preserved new file,
  commits with provenance, and attempts a guarded push, returning a structured
  `mac.new_file_recovery.v1` result.
- The recovery seams are injectable, so
  `tests/test_new_file_recovery_staging.py` exercises the logic without a live
  remote.

## Ground Truth: The Executor Prompt Names The Leftover Files

- `src/mac/executor_prompt.py:353` surfaces the exact new files left
  uncommitted and maps them to the `untracked_new_files_at_finalize` error
  signature (`src/mac/executor_prompt.py:376`), so the curated lesson tells the
  next agent to run `git add -A` and commit all new files up front instead of
  wasting an attempt on the same refusal
  (`tests/test_executor_prompt_finalizer.py:1`).

## Verification

Running the four suites on the current tree passes with a clean worktree. The
observed transcript:

```console
$ .venv/bin/python -m pytest -q \
    tests/test_worker_finalize_new_files.py \
    tests/test_git_finalizer_new_file_staging.py \
    tests/test_new_file_recovery_staging.py \
    tests/test_executor_prompt_finalizer.py
.........................
25 passed
```

## Disposition

No production code or test changes are warranted. The contract prerequisite is
satisfied by the existing implementation and its regression suites. This note is
the retained investigation evidence.
