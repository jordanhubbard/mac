---
id: mac-i044
status: closed
deps: []
links: []
created: 2026-05-27T00:55:17Z
type: bug
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-i044
---
# YAML workflow import has no size cap

src/mac/workflow_service.py:530-532 — import_yaml calls yaml.safe_load(yaml_text). safe_load blocks code execution but a 100MB nested-alias payload (billion-laughs style) still DoSes. No Content-Length check at the call sites. Fix: cap input size before parsing.

## Close Reason

Closed
