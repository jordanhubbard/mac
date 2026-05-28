---
id: mac-g55y
status: closed
deps: []
links: []
created: 2026-05-27T01:48:29Z
type: bug
priority: 0
assignee: Jordan Hubbard
mac-task-id: pending:mac-g55y
---
# ~/.mac/.env flat namespace collides tokens across fleets

~/.mac/.env stores fleet credentials under unscoped names like MAC_API_TOKEN, MAC_DEPLOY_HUB_TOKEN, MAC_DEPLOY_TOKENHUB_API_KEY. When a workstation participates in multiple fleets (e.g. jordanh-hub + rocky in deploy/fleet/config registry), the second fleet's setup overwrites the first fleet's credentials. Operator-visible symptom: tools authenticate against whichever fleet last wrote the .env. Fix: scope every fleet-bound env var by hub-name suffix, e.g. MAC_API_TOKEN__ROCKY, MAC_API_TOKEN__JORDANH_HUB, and update ~/.mac/.env writers + readers (setup.sh, scripts/setup-fleet.py, deploy_service, etc.) to look up the suffix matching the active fleet/hub. Migrate existing flat .env entries on first run by appending the new suffixed forms while keeping the old keys for one release for backward compat.

## Close Reason

Closed
