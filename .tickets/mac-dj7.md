---
id: mac-dj7
status: closed
deps: []
links: []
created: 2026-05-24T18:44:49Z
type: feature
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-dj7
---
# Make TokenHub MAC secret and routing authority

Adopt TokenHub as the early hub-side dependency for upstream provider/search/embed tokens and model routing. MAC deploy/startup should bootstrap TokenHub before AI/Hermes paths, migrate provider secrets out of MAC/host env files into hub TokenHub instances for rocky and jordanh-maxgpu, and add checks proving stale direct-provider secret paths are gone.

## Close Reason

Implemented TokenHub as MAC fleet secret/model authority, deployed to rocky and jordanh-maxgpu, verified vault-backed provider credentials and TokenHub-only runtime envs.
