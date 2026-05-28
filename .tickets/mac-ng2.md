---
id: mac-ng2
status: closed
deps: []
links: []
created: 2026-05-20T04:48:32Z
type: bug
priority: 0
mac-task-id: pending:mac-ng2
---
# Auto-review: no root of trust under verification manifest

services.py:2367 _assess_default_review_evidence accepts repo.head_sha, repo.pushed, tests[*].returncode as self-reported strings the executor writes into mac-evidence.json. The reviewer accepts them without any independent check. A compromised or buggy executor that knows the schema can self-approve by producing valid-looking JSON. For an autonomous swarm this is the entire security model — and there is no security model. Pick one of: (a) remote-attested execution — the executor's sandbox runtime signs the manifest with a registered key, reviewer verifies; (b) independent re-verification — reviewer agent fetches head_sha from the real remote and re-runs declared tests in its own sandbox; (c) witness quorum — N independent agents each produce matching evidence before approval. Today there is none of these.

## Close Reason

Closed
