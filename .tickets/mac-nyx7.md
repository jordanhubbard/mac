---
id: mac-nyx7
status: closed
deps: []
links: []
created: 2026-05-27T19:18:06Z
type: task
priority: 2
mac-task-id: pending:mac-nyx7
---
# Refresh TokenHub wildcard ladder from MAC weekly

Use the new TokenHub /admin/v1/wildcard-models API from MAC on a weekly schedule to refresh the ordered model ladder from current model availability/quality/cost data. Hermes should continue requesting model="*" through TokenHub keys, not provider OPENAI_API_KEY.

## Resolution (2026-05-31)

Weekly TokenHub wildcard-ladder refresh implemented. src/mac/tokenhub_wildcard.py (mirrors tokenhub_feed.py) resolves the /admin/v1/wildcard-models admin URL, fetches the ladder with the TokenHub admin token, normalizes the several response shapes, and records a tokenhub.wildcard.refresh observation (ladder capped at 50 to avoid bloat). Exposed as ControlPlane.refresh_tokenhub_wildcards + `mac tokenhub refresh-wildcards`. Weekly systemd timer + installer: deploy/systemd/mac-wildcard-refresh.{service,timer} + deploy/install-wildcard-refresh-service.sh. Gated like hu-05: clean no-op (status=skipped) unless TOKENHUB_URL/MAC_TOKENHUB_WILDCARD_URL and a TokenHub admin token are both set. tests/test_tokenhub_wildcard.py (6 tests). Hermes keeps requesting model='*' via TokenHub keys — unchanged. Activation: install the timer + provide MAC_TOKENHUB_ADMIN_TOKEN; if the deployed TokenHub treats the endpoint as a recompute trigger, set MAC_TOKENHUB_WILDCARD_METHOD=POST.
