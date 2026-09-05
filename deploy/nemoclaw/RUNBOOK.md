# NemoClaw Fleet-Wide YOLO Migration Runbook

**YOLO rescope (2026-07-04):** Fleet is pre-release, sole user, no compat
requirements.  Drop the pilot ceremony — this runbook covers full YOLO
migration of the chat-gateway role on all fleet hosts.

---

## Overview

| Item | Value |
|---|---|
| Gateway being replaced | hermes gateway (`<fleet>-hermes-gateway.service`) |
| Gateway being installed | NemoClaw (`<fleet>-nemoclaw-gateway.service`) |
| OpenShell required | 0.0.72 (NemoClaw hard pin) |
| Node required | 22+ (installed task-locally by install-nemoclaw-gateway.sh) |
| Executor impact | **None** — vendored Hermes executor stays (ADR 0001) |
| Host order | worker-1 → worker-2 → pods → hub (hub LAST) |

### What moves and what stays

| Component | Before | After |
|---|---|---|
| Chat-gateway service | `<fleet>-hermes-gateway.service` | `<fleet>-nemoclaw-gateway.service` |
| Chat-gateway binary | `~/.mac/bin/hermes-gateway` | `~/.mac/bin/nemoclaw-gateway` |
| Chat-gateway port | 8765 | 8765 (same port; hermes gateway disabled). During pilot phase port 18765 was used for coexistence; YOLO migration reclaims 8765. |
| Hermes home (chat) | `~/.hermes` | `~/.hermes` (reused as `~/.hermes-nemoclaw` alias in pilot; YOLO reuses `~/.hermes` directly) |
| Task executor | vendored Hermes (in-process Python) | **unchanged** |
| Executor Hermes home | `~/.hermes` | **unchanged** |
| Multi-slack patch | active (gateway-side dead code after migration) | follow-up task to prune |
| Gateway-side hermes patches | active | follow-up task to prune |

---

## Prerequisites

| Requirement | Check |
|---|---|
| OpenShell 0.0.72 | `openshell --version` → `0.0.72` |
| Docker Engine/Moby | `docker info` succeeds |
| Hub reachable | `curl http://<hub>:8789/healthz` → 200 |
| Slack app tokens | Socket Mode enabled; `xoxb-...` + `xapp-...` obtained |
| mac source tree | `$MAC_HOME/src/mac` contains this file |

If OpenShell is wrong version, run first:
```bash
OPENSHELL_VERSION=0.0.72 deploy/openshell/bootstrap-openshell.sh
```

### macOS and Docker Desktop storage risk

Do not deploy NemoClaw on a macOS host using Docker Desktop until its actual
state-storage paths have been identified and tested. NemoClaw uses the same
OpenShell-managed sandbox class and `mac-hermes.Containerfile` as OpenClaw. On
macOS, that sandbox's state mount can reside on containerd-snapshotter
overlayfs inside Docker Desktop's VirtioFS-backed VM.

POSIX advisory byte-range locking (`fcntl`/`flock`) has been observed to hang
indefinitely on that mount under a small concurrent SQLite WAL write test. It
can therefore expose applications that keep SQLite state there to hangs or
corruption. NemoClaw's own use of SQLite has not been confirmed, and this is a
deployment risk rather than a confirmed NemoClaw incident.

Before approving a macOS-hosted deployment:

1. Confirm whether NemoClaw stores any state in SQLite.
2. Resolve every such database path to its host and sandbox mount.
3. Run a bounded concurrent SQLite WAL locking probe on the actual mount.
4. If the mount is affected, bind-mount the state from a host path outside the
   Docker Desktop overlay or deploy NemoClaw on a non-macOS host.

See the upstream OpenClaw report for the reproduced failure mode:
https://github.com/openclaw/openclaw/issues/139214

---

## Host Migration Procedure

Run the following on each host in order: **worker-1 → worker-2 → pods → hub**.

If any host migration fails:
1. Leave its hermes gateway running (do **not** stop it).
2. Note the failure in the `## Host Status` table below.
3. Proceed to the next host.
4. File a follow-up task to repair the failed host.

### Environment (set before running on each host)

```bash
# Source mac.env for standard vars; then add nemoclaw-specific overrides.
source ~/.mac/mac.env

export MAC_NEMOCLAW_HUB_URL="http://<hub>:8789/v1"
export MAC_NEMOCLAW_AGENT_ID="agent_<hostname>"
export MAC_NEMOCLAW_INSTANCE_ID="hermes_<hostname>_nemoclaw"
export MAC_NEMOCLAW_SLACK_WORKSPACE="<slack-workspace-name>"
export MAC_NEMOCLAW_FLEET_NAME="mac"
export MAC_NEMOCLAW_HOME_CHANNEL="<home-channel-name>"

# Tokens from vault / host secret store — never hardcoded.
export MAC_NEMOCLAW_SLACK_BOT_TOKEN="$(cat ~/.mac/secrets/slack_bot_token)"
export MAC_NEMOCLAW_SLACK_APP_TOKEN="$(cat ~/.mac/secrets/slack_app_token)"
```

### Run the migration script

```bash
cd ~/.mac/src/mac
bash deploy/nemoclaw/install-nemoclaw-gateway.sh
```

The script is idempotent: safe to re-run after fixing a failure.

### Dry-run (preview without making changes)

```bash
NEMOCLAW_DRY_RUN=1 bash deploy/nemoclaw/install-nemoclaw-gateway.sh
```

---

## Verification (per host)

After the script completes, verify:

```bash
# 1. NemoClaw gateway service is active.
systemctl status mac-nemoclaw-gateway.service

# 2. Hermes gateway service is stopped and disabled.
systemctl status mac-hermes-gateway.service   # should show: inactive (dead)

# 3. Gateway log shows no fatal errors.
journalctl -u mac-nemoclaw-gateway.service --since "5 minutes ago" --no-pager

# 4. Executor sandbox still works (executor runs in-process hermes, not gateway).
~/.mac/venv/bin/python -c "import mac.hermes_gateway; print('executor hermes OK')"

# 5. Hub connectivity.
curl http://<hub>:8789/healthz
```

Send a mention in the configured Slack channel to verify end-to-end:
```
@<agent-name> ping
```

---

## Credential Audit

Per CREDENTIAL MIGRATION spec: record here which env vars map to which
credentials for teardown/rotation auditability.

| Credential | Source env var | Target config field |
|---|---|---|
| Slack bot token | `MAC_NEMOCLAW_SLACK_BOT_TOKEN` | `~/.hermes/slack_accounts.json[0].bot_token` |
| Slack app token | `MAC_NEMOCLAW_SLACK_APP_TOKEN` | `~/.hermes/slack_accounts.json[0].app_token` |
| Router API key | `OPENAI_API_KEY` / `MAC_API_TOKEN` in mac.env | nemoclaw-gateway wrapper `OPENAI_API_KEY` |
| Router x-mac-agent-id | `MAC_NEMOCLAW_AGENT_ID` | `~/.hermes/config.yaml static_headers` |
| Router x-mac-hermes-instance-id | `MAC_NEMOCLAW_INSTANCE_ID` | `~/.hermes/config.yaml static_headers` |

Token rotation: update the source (vault / host secret store) and re-run
`install-nemoclaw-gateway.sh` to re-render `slack_accounts.json` and
`config.yaml`.  No service restart is needed if only `config.yaml` changes
(Hermes hot-reloads on SIGUSR1 via `systemctl reload <svc>`).

---

## Rollback (per host)

```bash
sudo systemctl stop    mac-nemoclaw-gateway.service
sudo systemctl disable mac-nemoclaw-gateway.service
sudo systemctl enable  mac-hermes-gateway.service
sudo systemctl start   mac-hermes-gateway.service
# Verify hermes gateway is running.
systemctl status mac-hermes-gateway.service
```

The hermes gateway unit was backed up by the install script under
`~/.mac/backups/` before being replaced.  If the unit file was lost:

```bash
# Restore from deploy source.
sudo cp deploy/systemd/mac-hermes-gateway.service /etc/systemd/system/mac-hermes-gateway.service
sudo systemctl daemon-reload
sudo systemctl enable mac-hermes-gateway.service
sudo systemctl start  mac-hermes-gateway.service
```

---

## Host Status

Update this table as each host is migrated.

| Host | Role | Status | Notes |
|---|---|---|---|
| worker-1 | bare metal | pending | Migrate first |
| worker-2 | spoke | pending | |
| pods | spoke | pending | |
| hub | hub | pending | Migrate LAST |

---

## Follow-up Tasks (post-migration)

Once all hosts are migrated:

1. **Prune dead code**: the multi-slack patch and gateway-side hermes patches
   become dead code once all hosts are on NemoClaw.  File a task to remove:
   - `deploy/hermes/multi-slack-mvp.patch`
   - `deploy/hermes/mac-runtime-context-prompt.patch`
   - `deploy/hermes/mac-provider-decision.patch`
   - `deploy/hermes/post-snapshot-mac-fixes.patch`
   - `deploy/hermes/disable-shutdown-chat-notices.patch`
   - The hermes gateway service from `deploy/deploy-mac-fleet.sh` install path.

2. **Update fleet config defaults**: set `hermes.gateway_impl: nemoclaw` as
   the default in `deploy/fleet/config.yaml`.

3. **Update this runbook** with final per-host migration timestamps.

---

## Architecture (post-migration)

```
Each fleet host
  ├── <fleet>-nemoclaw-gateway.service  (NemoClaw chat gateway — replaces hermes gateway)
  │     └── ~/.mac/bin/nemoclaw-gateway
  │           └── OpenClaw sandbox
  │                 └── mac-hermes:net runtime image
  │                       └── Hermes (vendored, for the chat session context only)
  │                             └── MAC router (http://<hub>:8789/v1)
  │
  ├── mac-openshell-supervisor.service   (task executor parent — UNCHANGED)
  │     └── mac-hermes-task-executor     (in-process Python, ADR 0001 — UNCHANGED)
  │           └── vendored Hermes        (task executor — NOT migrating)
  │
  └── mac.service                        (control plane — UNCHANGED)
```

The executor's `~/.hermes` home and its Hermes process are **completely
separate** from the NemoClaw gateway.  The gateway replacing hermes does not
affect task execution.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `openshell: command not found` | OpenShell not installed | Run `bootstrap-openshell.sh` with `OPENSHELL_VERSION=0.0.72` |
| `OpenShell X.Y.Z installed but NemoClaw requires 0.0.72` | Wrong version | Re-run bootstrap with correct version |
| Gateway exits immediately | Missing `mac-runtime-context.md` | Check `~/.hermes/mac-runtime-context.md` |
| `policy file not found` | OpenClaw policy not installed | Check `~/.mac/openshell/<agent-id>-policy.yaml` |
| No Slack events | Wrong `app_token` or Socket Mode off | Verify Slack app config |
| `401 Unauthorized` from router | Wrong `OPENAI_API_KEY` | Refresh from mac vault |
| Executor broken after migration | Shared Hermes home conflict | Check `HERMES_HOME` env in executor vs gateway |
| Port 8765 already in use | Another process on that port | `lsof -i :8765` to identify; set `MAC_NEMOCLAW_GATEWAY_PORT` |
