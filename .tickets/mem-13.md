---
id: mem-13
status: open
deps: []
links: [mem-01, mem-11, mem-12]
created: 2026-05-29T02:58:04Z
type: feature
priority: 2
assignee:
mac-task-id: task_267e376dc8da49b1b45b5c9672942c27
audit: memory-tier-2026-05-28
discovered_via: mem-01
---
# Validator: verify pushed remote_ref resolves on remote

**Discovered in mem-01 root cause.** Bullwinkle's evidence manifest declared `repo.pushed=false` and was accepted anyway via the `operator_result` softpath (mem-11 handles that). But there's a deeper validator gap: when an executor lies and says `pushed=true, remote_ref=<x>`, **the current validator does not verify that `<x>` actually resolves on the remote**. It just checks string format.

`RepoChangeValidator.require_pushed_repo_anchor()` in `src/mac/evidence_validators.py:77`:
```python
if not (repo.pushed and repo.remote_ref) and not repo.pr_url:
    problems.append("repo evidence requires pushed=true with remote_ref, or pr_url")
```
That's the only check. A future executor (intentionally or accidentally) saying `pushed=true, remote_ref=refs/heads/whatever` with no actual push would pass and trigger the same review-loop pathology.

## Acceptance Criteria

- When evidence claims `pushed=true && remote_ref`, the validator (or a deferred async verifier) runs `git ls-remote <remote_url> <remote_ref>` and rejects the evidence if the ref does not resolve.
- When evidence claims `pr_url`, same idea: GET the URL and verify it exists.
- This check is best-effort online; offline mode falls back to recording a `verification_pending` marker and re-checking on the next worker cycle.
- Test: submit evidence with a bogus remote_ref; assert it is rejected (or marked pending and rejected on re-check).

Discovered during [mem-01](mem-01.md) root cause analysis on 2026-05-29.
