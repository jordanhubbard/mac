---
id: mem-11
status: open
deps: []
links: [mem-01, mem-12, mem-13]
created: 2026-05-29T02:58:00Z
type: bug
priority: 1
assignee:
mac-task-id: task_f760d5a4169847dfb6f0bfcbc179eda2
audit: memory-tier-2026-05-28
discovered_via: mem-01
---
# Reject operator_result evidence for repo-coupled tasks

**Discovered in mem-01 root cause.** The executor for `task_d7c51a0b...` ("End-to-end proof v4 mechanical: autonomous merge via mac task") emitted evidence with `evidence_type=operator_result`. The validator accepted it because `OperatorResultValidator.validate()` (`src/mac/evidence_validators.py:222`) only requires *any* summary string — no repo anchor, no `files_changed`, no pushed-ref.

The dispatcher knows whether a task is repo-coupled (it stages a worktree, a branch, etc. — see `task.evidence_added` history showing `repository_required` and `repository_branch`). So at evidence-write time the system has enough information to reject the wrong evidence type.

## Acceptance Criteria

- When the task's execution contract declared `repository_required=true` (or the dispatcher staged a branch/worktree for it), evidence with `evidence_type=operator_result` is rejected at write time with a clear error.
- Acceptable evidence types for repo-coupled tasks: `repo_change`, `documentation`, `test`, `artifact`, `no_change`, `review_verdict`. (i.e. anything that uses `require_pushed_repo_anchor()`.)
- Test: simulate bullwinkle's manifest against a repo-coupled task; assert `record_evidence` raises ValidationError.
- Existing inert / non-repo tasks remain free to use `operator_result`.

Discovered during [mem-01](mem-01.md) root cause analysis on 2026-05-29.
