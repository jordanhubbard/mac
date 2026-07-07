# Stock OpenClaw gateway under MAC OpenShell policy

This is MAC's primary chat-gateway implementation. It runs the official stock
OpenClaw image inside a MAC-owned OpenShell sandbox. `deploy/nemoclaw/` remains
reference material and is not invoked by this path.

Task execution is a separate MAC worker role. Migrating this gateway does not
change the executor or authorize deletion of `src/mac/_hermes`.

## Pinned runtime

- OpenClaw: `2026.6.11`
- Source image: `ghcr.io/openclaw/openclaw:2026.6.11`
- Manifest digest: `sha256:3814fb1f62f9cfc5944de088c5817c68c88b5d721feebe36420b666a90a61ce7`
- Local image: `localhost/mac-openclaw:2026.6.11`
- OpenShell: the MAC fleet pin installed by `deploy/openshell/bootstrap-openshell.sh`

The official image is extended only with OpenShell's required non-root
`sandbox` identity and MAC's state-directory layout.

## Host-local inputs

The installer reads the deployed `~/.mac/mac.env` and `~/.hermes/.env`. The
following values must be available, either through their existing MAC/Hermes
names or explicit `MAC_OPENCLAW_*` overrides:

- agent and Hermes-instance identity;
- MAC router URL, API key, and selected model;
- Slack Socket Mode bot and app tokens.

The generated `openclaw.json` contains SecretRefs and `${ENV}` references, not
credential values. Actual values are written only to
`~/.mac/openclaw/managed/runtime.env` with mode `0600`, then uploaded into the
sandbox. They are never passed through process argv.

## Prepare without changing services

```bash
deploy/openclaw/install-openclaw-gateway.sh prepare
```

This operation is idempotent. It renders the policy and configuration, builds
the pinned image if absent, writes the host wrapper, and preserves the existing
gateway token. If an older OpenClaw sandbox must be replaced, its state and
workspace are downloaded to the owner-only `~/.mac/openclaw/backups/` tree
before deletion.

## Fleet deployment

Set `hermes.gateway_impl: openclaw` in the fleet configuration and deploy in
this order:

1. Bullwinkle canary.
2. Natasha.
3. GKE worker and hub nodes.
4. Rocky hub last.

`deploy/deploy-mac-fleet.sh` supports systemd, supervisord, and launchd. It
starts OpenClaw, then requires all of these checks before disabling Hermes:

- `/healthz` liveness;
- `/readyz` readiness;
- `openclaw config validate --json`;
- authenticated gateway health RPC;
- live Slack channel probe.

For a canary, additionally export `MAC_DEPLOY_OPENCLAW_LIVE_CANARY=1`. The
verification command performs a real model turn and requires the sentinel
`MAC_OPENCLAW_CANARY_OK`.

Hermes is disabled only after verification succeeds. It remains installed as
the rollback gateway until the separate Hermes-retirement chain completes. A
failed verification invokes rollback automatically.

## Manual verification

```bash
MAC_OPENCLAW_LIVE_CANARY=1 \
  deploy/openclaw/install-openclaw-gateway.sh verify
```

Useful secret-free checks:

```bash
openshell sandbox get mac-openclaw-<agent>
curl -fsS http://127.0.0.1:18789/healthz
curl -fsS http://127.0.0.1:18789/readyz
```

OpenClaw documents `/healthz` as liveness and `/readyz` as the stricter usable
readiness check: <https://docs.openclaw.ai/cli/gateway>.

## Rollback

```bash
MAC_OPENCLAW_FLEET_NAME=mac \
MAC_OPENCLAW_SUPERVISOR=auto \
  deploy/openclaw/install-openclaw-gateway.sh rollback
```

Rollback stops and disables the OpenClaw supervisor entry and restores the
retained Hermes gateway. It deliberately preserves the OpenClaw sandbox,
configuration, and state for diagnosis or a corrected retry.

## Updating OpenClaw

Update all three pins together:

1. official tag and manifest digest in `OpenClaw.Containerfile`;
2. `OPENCLAW_VERSION` in `install-openclaw-gateway.sh`;
3. this runbook.

Run the focused deployment tests, stock OpenClaw config validation, a canary,
and the full repository contract before fleet promotion. Never replace the
digest with `latest` in a production path.
