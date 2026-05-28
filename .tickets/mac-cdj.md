---
id: mac-cdj
status: closed
deps: []
links: []
created: 2026-05-19T08:27:02Z
type: bug
priority: 1
mac-task-id: pending:mac-cdj
---
# Guard stale worker completions after lost lease

Live fleet workers exposed a race where an expired lease let a task be reclaimed while the old executor was still running; the stale executor could then mark the task failed after it no longer owned the lease. Add a worker guard so stale completions are recorded as telemetry and do not mutate the task.

## Close Reason

fixed
