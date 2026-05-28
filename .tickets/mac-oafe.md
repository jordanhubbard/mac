---
id: mac-oafe
status: closed
deps: []
links: []
created: 2026-05-27T19:31:25Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-oafe
---
# Make setup.sh a one-pass fleet setup and deployment entrypoint

MAC setup is split across setup.sh and deploy/deploy-mac-fleet.sh, and the setup skill still tells users to run two passes. Merge the UX into setup.sh so a hub or worker can go from zero to configured and deployed in one command, and fix TokenHub release-binary installation so setup prefers published binaries before source builds.

## Close Reason

setup.sh now writes a deploy plan and immediately deploys hub/worker setups by default; TokenHub release installer handles published archives.
