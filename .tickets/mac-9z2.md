---
id: mac-9z2
status: closed
deps: []
links: []
created: 2026-05-18T05:53:21Z
type: task
priority: 2
mac-task-id: pending:mac-9z2
---
# Environment model bridging artifact → rollout → deployment

## Close Reason

environments(name, tenant_id, channel, promotes_from) + deployments(env, artifact, status, retired_at) + environment_events. deploy_artifact atomically retires the prior active deployment and installs the new one inside BEGIN IMMEDIATE. environment events flow through the unified /events stream (subject_type='environment'). API /environments + /environments/{id}/deploy|current|deployments. CLI 'mac env register|list|show|deploy|current|history'. Tests cover retire-and-replace atomicity, validation, tenant/channel filtering, and event stream integration.
