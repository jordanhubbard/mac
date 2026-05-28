---
id: mac-nyx7
status: open
deps: []
links: []
created: 2026-05-27T19:18:06Z
type: task
priority: 2
mac-task-id: pending:mac-nyx7
---
# Refresh TokenHub wildcard ladder from MAC weekly

Use the new TokenHub /admin/v1/wildcard-models API from MAC on a weekly schedule to refresh the ordered model ladder from current model availability/quality/cost data. Hermes should continue requesting model="*" through TokenHub keys, not provider OPENAI_API_KEY.
