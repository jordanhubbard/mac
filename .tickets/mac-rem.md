---
id: mac-rem
status: closed
deps: [mac-upk]
links: []
created: 2026-05-20T05:52:02Z
type: bug
priority: 0
mac-task-id: pending:mac-rem
---
# Documentation evidence escape hatch lets unpushed work pass review

`_verification_type_problems` for `documentation` evidence (services.py:2722-2732) has an explicit escape hatch:

```
if evidence_type == 'documentation':
    repo_problems = self._repo_verification_problems(manifest, require_tests=False)
    if not repo_problems:
        return []
    artifacts = _manifest_list(manifest.get('artifacts'))
    if artifacts and self._passed_verification_check_count(manifest) > 0:
        return []
    return ['documentation evidence requires a pushed repo artifact or explicit artifacts plus passing checks']
```

The `artifacts + passing checks` fallback means a documentation task can pass without ever pushing a commit — the agent declares 'I made a doc artifact URL X and ran a check Y' and the gate opens. Same root cause as the broader taxonomy gap: the system trusts agent-self-reported artifact URIs without anchoring them to a witnessed commit.

Fix: drop the alternative path. Documentation evidence requires `verification.repo.head_sha` + `pushed=true` like every other type. If a doc isn't worth committing, it isn't worth recording as completed work in this system.

Acceptance: test `test_documentation_evidence_rejected_without_pushed_repo` exists. The existing call shape `self._repo_verification_problems(manifest, require_tests=False)` becomes the sole gate.

## Close Reason

Removed the documentation artifact escape hatch; documentation evidence now goes through the same pushed repo anchor helper as repo_change, with regression coverage in the all-evidence-types review-readiness test.
