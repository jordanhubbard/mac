# NemoClaw Pilot Runbook

Single-host OpenClaw gateway pilot running alongside the existing hermes
gateway service.  This runbook is fleet-generic: substitute all
`<placeholder>` values for your fleet's real values at deploy time.

---

## Architecture

```
Host: <host>
  ├── hermes gateway (existing, port 8765, ~/.hermes)
  └── nemoclaw gateway (pilot, port 18765, ~/.hermes-nemoclaw)
        └── OpenClaw sandbox
              └── mac-hermes:net runtime image
```

Both gateways share the MAC router on the hub (`http://hub.example.internal:8789/v1`)
but use distinct HERMES_HOME directories, Slack workspaces, and MAC
hermes-instance IDs.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| Docker Engine / Moby | MAC standardizes on Docker Engine; do not use Podman |
| OpenClaw installed | `openshell --version`; install via fleet deploy or bootstrap-openshell.sh |
| mac runtime image built | `docker inspect localhost/mac-hermes:net` must succeed |
| Hub reachable | `curl http://hub.example.internal:8789/healthz` returns 200 |
| Slack app configured | Socket Mode enabled, bot + app tokens obtained |

---

## Initial Setup

### 1. Build the runtime image (if not already built)

```bash
cd <mac-src>
docker build -t localhost/mac-hermes:net \
  -f deploy/openshell/mac-hermes.Containerfile .
```

### 2. Create the pilot Hermes home directory

```bash
NEMOCLAW_HOME="${HOME}/.hermes-nemoclaw"
mkdir -p "${NEMOCLAW_HOME}"
chmod 0700 "${NEMOCLAW_HOME}"
```

### 3. Install the provider config

```bash
cp deploy/nemoclaw/config.yaml "${NEMOCLAW_HOME}/config.yaml"
# Edit: replace all <placeholder> values
"${EDITOR:-vi}" "${NEMOCLAW_HOME}/config.yaml"
```

### 4. Install the Slack account credentials

```bash
# Copy the example and fill in real tokens (bot_token, app_token).
# NEVER commit the filled-in file.
cp deploy/nemoclaw/slack-account.example.json \
  "${NEMOCLAW_HOME}/slack_accounts.json"
chmod 0600 "${NEMOCLAW_HOME}/slack_accounts.json"
"${EDITOR:-vi}" "${NEMOCLAW_HOME}/slack_accounts.json"
```

Obtain the tokens from https://api.slack.com/apps:
- **bot_token** (`xoxb-...`): OAuth & Permissions → Bot User OAuth Token
- **app_token** (`xapp-...`): Basic Information → App-Level Tokens
  (requires the `connections:write` scope)

### 5. Write the MAC runtime context file

The MAC fleet deploy normally writes this file automatically.  For a
manual pilot setup, create it by hand:

```bash
cat > "${NEMOCLAW_HOME}/mac-runtime-context.md" << 'EOF'
## NemoClaw Pilot Context

- Agent: <agent-id>
- Fleet: <fleet-name>
- Role: nemoclaw-gateway
- Hub: http://hub.example.internal:8789
- Pilot Slack workspace: <workspace-name>
EOF
```

### 6. Install the OpenClaw sandbox policy

The policy file for this agent must exist before the gateway starts.
The MAC fleet deploy writes it to `~/.mac/openshell/<agent-id>-policy.yaml`.
For a manual pilot, copy and customize the template:

```bash
mkdir -p "${HOME}/.mac/openshell"
cp deploy/openshell/mac-hermes-policy.yaml \
  "${HOME}/.mac/openshell/<agent-id>-policy.yaml"
# Substitute __PLACEHOLDER__ tokens for your fleet's values.
"${EDITOR:-vi}" "${HOME}/.mac/openshell/<agent-id>-policy.yaml"
```

### 7. Start the NemoClaw gateway

```bash
NEMOCLAW_HERMES_HOME="${HOME}/.hermes-nemoclaw" \
MAC_HOME="${HOME}/.mac" \
docker compose \
  -f deploy/nemoclaw/docker-compose.yaml \
  up -d
```

---

## Verification

```bash
# Check the container is running
docker compose -f deploy/nemoclaw/docker-compose.yaml ps

# Tail the gateway log
docker compose -f deploy/nemoclaw/docker-compose.yaml logs -f

# Confirm it listens on the pilot port
curl -s http://127.0.0.1:18765/healthz || echo "healthz not exposed — check gateway log"

# Confirm the existing hermes gateway is still running on its original port
# (replace 8765 with the actual port if it differs on your host)
curl -s http://127.0.0.1:8765/healthz && echo "existing gateway OK"
```

Send a mention in the configured Slack channel to verify end-to-end
connectivity:

```
@nemoclaw-agent ping
```

The agent should respond.  If it does not, check:

1. `docker compose logs nemoclaw-gateway` — look for connection errors.
2. Confirm `slack_accounts.json` has the correct `bot_token` and `app_token`.
3. Confirm the Slack app has Socket Mode enabled and the app token scope
   includes `connections:write`.

---

## Upgrading the Runtime Image

```bash
# 1. Rebuild the image from the updated mac source tree.
cd <mac-src>
docker build -t localhost/mac-hermes:net \
  -f deploy/openshell/mac-hermes.Containerfile .

# 2. Record the new image digest.
docker inspect localhost/mac-hermes:net \
  --format '{{index .RepoDigests 0}}'
# Update the digest comment in docker-compose.yaml with the new value.

# 3. Restart the pilot gateway to pick up the new image.
docker compose -f deploy/nemoclaw/docker-compose.yaml up -d --force-recreate
```

---

## Stopping and Removing

```bash
# Stop (preserve volumes and config):
docker compose -f deploy/nemoclaw/docker-compose.yaml down

# Full teardown (also removes volumes):
docker compose -f deploy/nemoclaw/docker-compose.yaml down -v
```

The existing hermes gateway service is unaffected by either command.

---

## Coexistence Notes

- NemoClaw uses a **separate HERMES_HOME** (`~/.hermes-nemoclaw`) and a
  **separate Slack workspace**; it does not share config or state with
  the existing hermes gateway.
- NemoClaw listens on port **18765**; the existing hermes gateway on
  port **8765** (or your configured port).  Both can run concurrently.
- Both gateways route model calls through the same MAC router hub URL
  (`http://hub.example.internal:8789/v1`) but use distinct
  `x-mac-hermes-instance-id` headers so the router attributes usage
  correctly.
- Do not modify `deploy/hermes/` or any existing service files as part
  of the NemoClaw pilot.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Gateway exits immediately | Missing `mac-runtime-context.md` | Write the context file (Step 5) |
| `policy file not found` | OpenClaw policy not installed | Install policy (Step 6) |
| No Slack events received | Incorrect `app_token` or Socket Mode not enabled | Verify Slack app config |
| `401 Unauthorized` from router | Wrong `OPENAI_API_KEY` or expired token | Refresh token from mac vault |
| Port 18765 already in use | Another service on that port | Change `MAC_NEMOCLAW_GATEWAY_PORT` in docker-compose.yaml |
| Existing hermes gateway fails after deploy | NemoClaw modified a shared file | NemoClaw must not modify shared config; check for accidental bind-mount overlap |
