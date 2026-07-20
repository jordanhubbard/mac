!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Prerequisite Verification: task_403ed263ed7e45c6b7624345005a097c

**Task**: Verify and repair toolchain + bootstrap prerequisite for
`openclaw_fleet_rollout`.
**Parent task**: task_ea6f9ec1ac1e49a0b1d11fe536fd8b84 (goal: implement
`src/mac/openclaw_fleet_rollout.py`).
**Plan node**: `toolchain_bootstrap`
**Verified by**: fleet worker (prerequisite verification; no deliverable edits)
**Verification date**: 2026-07-16

## Status: VERIFIED-HEALTHY

The execution-environment prerequisite required by the parent task is present
and usable. The contract toolchain, the bootstrap-produced virtualenv, and the
deliverable test suite were all confirmed in the task-owned worktree. No
deliverable source or test file was modified.

## Toolchain Commands

Measured with `<command> --version` in the task worktree; all commands are on
`PATH` and satisfy the contract requirements.

| Command  | Version found | Requirement                                   | Result |
|----------|---------------|-----------------------------------------------|--------|
| python3  | 3.12.13       | present (>= 3.11 for bootstrap)               | OK     |
| git      | 2.39.5        | >= 2.38 for `merge-tree`/`write-tree`         | OK     |
| gh       | 2.95.0        | present                                       | OK     |

`git write-tree` and `git merge-tree --write-tree` both succeed, so the
merge-gate portion of the contract suite (and the production merge queue) can
run on this host.

## Bootstrap

Command: `python3 scripts/bootstrap-project.py`

The bootstrap completed successfully (exit 0) and the contract-required
artifacts are present and executable:

- `.venv/bin/python` (symlink to `python3.12`)
- `.venv/bin/pytest`
- `.venv/bin/coverage`

The editable `mac` package installs and imports from the worktree
(`src/mac/__init__.py`), so tests exercise the checked-out code.

## Deliverable Suite Health

- `import mac` succeeds against the worktree source.
- `.venv/bin/pytest tests/test_openclaw_fleet_rollout.py -q` collects and
  passes **52 tests** (`52 passed`).

## Drift / Sync

Runtime metadata for this lease reports `repository_ahead: 0` and
`repository_behind: 0` against `repository_base_sha`
`57f60328d1c129994d31d1cdd627225b030c29e2`. The task-owned worktree is a clean,
usable checkout. Canonical synchronization and any rebase are owned by the
deterministic host finalizer; the deliverable files
(`src/mac/openclaw_fleet_rollout.py`, `tests/test_openclaw_fleet_rollout.py`)
are unaffected by upstream drift because their contract (public symbols and the
52-test behavior suite) is self-contained and passes in this worktree.

## Assumptions

- This is a `repo_change` prerequisite task; the environment was already
  healthy, so the tracked deliverable is this verification record rather than a
  toolchain repair. Recorded here so the prerequisite state is auditable from
  the repository history.
