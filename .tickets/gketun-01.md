---
id: gketun-01
status: closed
resolved_by: "#59"
deps: []
links: [gketun-02, gketun-03]
created: 2026-06-03T00:00:00Z
type: bug
priority: 2
audit: jordanh-GKE-supervisord-validation
discovered_via: fresh-fleet-deploy
---
# network=none reverse tunnel goes FATAL on first deploy with no auto-recovery

## Symptom

Deploying a fresh `network: none` fleet (hub + spokes) leaves spokes
unregistered after the first deploy: `WARNING: hub tunnel not reachable after
first deploy; redeploy to complete setup`. The hub's reverse-tunnel program is
`FATAL  Exited too quickly`.

## Root cause

`deploy/deploy-mac-fleet.sh`: in the per-spoke orchestration, `install_reverse_tunnel_on_hub`
(~line 4945) runs **before** `deploy_host` (~4958), but `deploy_host` is what
authorizes the hub's tunnel pubkey on the spoke (`install_hub_tunnel_pubkey`).
So when the hub's tunnel program first starts, the spoke hasn't authorized the
key yet → the `ssh -R …` exits immediately → supervisord exceeds `startretries`
→ `FATAL`. `autorestart=true` does not recover a FATAL program, so even after
`deploy_host` authorizes the key the tunnel never reconnects. The post-deploy
"waiting for hub tunnel to auto-establish" loop therefore always times out on a
first deploy.

## Fix options

- Install/start (or `supervisorctl restart`) the hub tunnel **after** the spoke
  has authorized the pubkey (i.e. after `deploy_host`), not before; or
- give the tunnel program effectively-unlimited retries (`startretries`) +
  longer `startsecs` so it keeps retrying until the key lands; and/or
- in the post-deploy step, explicitly `restart` the hub tunnel program (clears
  FATAL) once the spoke key is in place, instead of relying on `autorestart`.

## Validated workaround (manual)

After the spoke authorized the key, `sudo supervisorctl restart
<fleet>-tunnel-<worker>` on the hub brought both tunnels to RUNNING and the
spokes reached the hub at `127.0.0.1:18789` (200).

## Resolution

Resolved by #59 (`startretries=1000` on the supervisord tunnel program). Validated 2026-06-03: both jordanh-GKE worker tunnels reachable in 5s on a clean redeploy — no FATAL, no second deploy needed.
