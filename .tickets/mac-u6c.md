---
id: mac-u6c
status: closed
deps: []
links: []
created: 2026-05-22T07:38:54Z
type: task
priority: 2
assignee: Jordan Hubbard
mac-task-id: pending:mac-u6c
---
# Keep Hermes deploy on available Python

Commit the approved deploy fallback change that stops installing python3.11 via deadsnakes during fleet deploy and installs Hermes with --ignore-requires-python when only the selected Python is available.

## Close Reason

Approved deploy change is present in local commit 5785b00; verified deploy/deploy-mac-fleet.sh with bash -n and git diff --check.
