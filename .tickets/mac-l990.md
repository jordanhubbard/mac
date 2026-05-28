---
id: mac-l990
status: closed
deps: []
links: []
created: 2026-05-27T21:11:10Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-l990
---
# Install TokenHub release binaries atomically

During rocky deploy, install-tokenhub-service.sh attempted to overwrite /home/jkh/.local/bin/tokenhub while mac-tokenhub.service still had the binary mapped, causing OSError: [Errno 26] Text file busy. The installer recovered by falling back to a source build, but release installation should write binaries to temporary files and atomically rename them into place after stopping or before restarting the service.

## Close Reason

TokenHub installer now stages release/source binaries, moves existing binaries to an old-binary directory, moves staged binaries into place, restarts/waits for TokenHub, then removes displaced binaries. Verified live on rocky: release install path succeeded without Text file busy, pending cleanup was absent, service active/healthy.
