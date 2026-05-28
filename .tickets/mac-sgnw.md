---
id: mac-sgnw
status: closed
deps: []
links: []
created: 2026-05-27T06:28:54Z
type: bug
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-sgnw
---
# Cache-bust dashboard auth prompt bundle

Rocky still shows the old passive dashboard token error after the auth prompt fix was deployed. The HTML references /ui/assets/app.js without a version, so browsers can keep the stale module. Add an asset version query for the dashboard bundle and verify the served UI loads the new auth prompt code.

## Notes

Rocky also exposes a huge dashboard/state payload: a valid scoped token returns 200 but produces about 155 MB and takes about 45 seconds. Fix covers cache-busting the browser bundle and compacting initial dashboard state while leaving full task timeline and default message list behavior available.

## Close Reason

Fixed: dashboard shell now cache-busts app.js, /dashboard/state returns compact task/message data, full timeline and default message APIs remain available, and regression coverage passes.
