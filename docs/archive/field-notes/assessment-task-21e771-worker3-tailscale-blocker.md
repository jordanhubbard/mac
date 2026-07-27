!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Assessment: task_21e77194d5fe4fd3963b8b1a61ece9d8

**Task**: Confirm the worker-3 (pending fleet worker node) rollout blocker before the
parent rotation task `task_62189cea1cb1457a950881da1ce6d53b`
("Rotate Tailscale key and finish worker-3 rollout") mutates any network or
secret state. Establish ground truth for three claims without mutating network
state and without handling the secret value:

1. worker-3 reaches its authoritative bastion route and runs the MAC/OpenShell
   toolchain plus the supervised Tailscale 1.98.9 daemon.
2. The hub route `<hub-mesh-ip>:8789` is currently unreachable over `tailscale0`.
3. The current `MAC_DEPLOY_TAILSCALE_AUTH_KEY` is rejected by the Tailscale
   control plane as invalid (capture the rejection *class*, not the key value).

**Repo areas reviewed** (read-only): `deploy/deploy-mac-fleet.sh`
(`prepare_network_prerequisites`, `classify_network_prerequisites`,
`probe_remote_hub_tcp`, `prepare_remote_tailscale_prerequisite`,
`fleet_scoped_env`), `deploy/install-tailscale.sh`, `src/mac/fleet_env.py`,
`src/mac/fleet_setup.py`, `deploy/fleet/config.yaml`.
**Assessment date**: 2026-07-27
**Assessed by**: fleet worker (investigation only; no production edits, no
network/secret mutation, no key value placed/echoed/logged anywhere).

## Scope boundary and evidence honesty

This confirmation node ran inside the task-owned git worktree sandbox. That
sandbox has **no `tailscale`, no `ssh`, no fleet secret material, and no route
to worker-3, the hub, or the Tailscale control plane** (verified:
`command -v tailscale` and `command -v ssh` both return 127; no
`MAC_DEPLOY_TAILSCALE_AUTH_KEY*` in the environment). The three runtime claims
are therefore **not directly re-measurable from this checkout** and are NOT
asserted here as freshly observed live facts. What this note establishes is the
**code-path ground truth** that (a) pins exactly where each claim is proven at
runtime, (b) fixes the exact operator probes that must be run *from the hub /
operator workstation* to confirm the three claims, and (c) states the exact
preconditions and acceptance checks the rotation must satisfy. This keeps the
parent task from mutating state on an unverified premise while never touching
the secret or the network.

## How the `auth_key_env` credential is resolved (verified in source)

The credential source is a *variable name*, not a value, at every checked-in
layer. Resolution is fleet-scoped with a legacy flat fallback.

- `deploy/fleet/config.yaml` -> `defaults.network.provider: none` (sample),
  `defaults.network.tailscale.auth_key_env: MAC_DEPLOY_TAILSCALE_AUTH_KEY`.
  The comment block is explicit: real topology lives in `~/.mac/fleets.yaml`
  and **no bearer tokens/provider keys belong in YAML**. So the checked-in file
  only names the env var; the value is supplied out-of-band.
- `src/mac/fleet_setup.py` `_network_config()` defaults `auth_key_env` to
  `MAC_DEPLOY_TAILSCALE_AUTH_KEY` when unset. In the render path (provider ==
  `tailscale`) it reads `secrets_block[auth_key_env]` (or `tailscale_auth_key`),
  and if present writes it into `env_values[auth_key_env]`. If absent and
  `require_mesh_auth` is true, the var name is appended to `required_env`
  (surfaces as a missing-credential setup error, not a control-plane error).
  `_looks_like_tailscale_auth_key()` is a **format-only** check
  (`tskey-`/`tskey-auth-` prefix + >=8 chars of remainder); a mismatch adds a
  *warning* ("does not look like a valid tskey- ... rotate it before deploy or
  mesh join will fail"). It is explicitly documented as NOT a liveness probe and
  never contacts the control plane. A key that is well-formed but
  expired/revoked/wrong-tailnet passes this check.
- `src/mac/fleet_env.py` provides the runtime resolver. `resolve()` /
  `resolve_first()` look up `MAC_DEPLOY_TAILSCALE_AUTH_KEY__<FLEET>` first
  (scoped, `<FLEET>` = fleet name uppercased with non-alphanumerics -> `_`,
  e.g. the fleet-scoped form), then the legacy flat
  `MAC_DEPLOY_TAILSCALE_AUTH_KEY` with a one-time mac-g55y deprecation warning.
  `MAC_DEPLOY_TAILSCALE_AUTH_KEY` is a member of `FLEET_SCOPED_VARS`, so both
  forms are migrated/warned consistently.
- `deploy/deploy-mac-fleet.sh` `fleet_scoped_env(key, fleet)` mirrors that
  order in shell: scoped env var -> flat env var -> `resolve_secret_from_store`
  for the scoped name -> then the flat name (encrypted vault / SecretsService).
  `prepare_network_prerequisites()` reads `auth_key_env` (spec field 34,
  defaulting to `MAC_DEPLOY_TAILSCALE_AUTH_KEY`), validates the *name* matches
  `^[A-Za-z_][A-Za-z0-9_]*$`, then resolves the value via `fleet_scoped_env`.
  Availability is checked only for emptiness / embedded newline/CR; there is no
  validity/liveness check here either.

Net: the deploy tooling proves the credential *exists and is well-named*; it
never proves the credential is *accepted*. Acceptance is decided solely by the
Tailscale control plane at `tailscale up` time.

## Where the rejection actually surfaces (verified in source)

The control-plane verdict is reached only in `deploy/install-tailscale.sh`,
which `prepare_remote_tailscale_prerequisite()` uploads to the node and runs
with the credential streamed over SSH stdin (`IFS= read -r
MAC_DEPLOY_TAILSCALE_AUTH_KEY`), never as an argv/log field. In cloud mode the
join is:

    run_tailscale $(tailscale_socket_flag) up \
      --auth-key="$TAILSCALE_AUTH_KEY" --hostname=... --accept-routes \
      --accept-dns=true >/dev/null 2>&1 || {
        echo "[tailscale] ERROR: tailscale join failed (credential-bearing output suppressed)" >&2
        exit 1
      }

Critical property for this task: **`tailscale up` stdout/stderr is redirected to
`/dev/null` on purpose** so that a failed join cannot print an auth-bearing
suggested command into the deploy log. The deploy log therefore shows only the
generic "tailscale join failed (credential-bearing output suppressed)" line,
then `exit 1`. The *machine-readable control-plane rejection reason* is NOT in
the deploy log by design; it is only observable on the node via the tailscaled
backend (e.g. `tailscale status --json` -> `BackendState`/`AuthURL`, or the
`tailscaled.log`/`journalctl` for the supervised daemon). When the join never
completes, `wait_for_tailscale_ip()` returns empty and the script exits 1 after
`tailscale status` to stderr.

Consequently the blocker manifests downstream as an **unreachable hub route**:
without a successful mesh join the node has no `tailscale0` path to the hub, so
`classify_network_prerequisites()` / `probe_remote_hub_tcp()` (a plain TCP
`socket.create_connection((host, port), 5)` to the hub_url host:port over the
node's routing table) fails, and the deploy refuses cutover with the
"hub route prerequisite is unreachable" error pointing operators at
`--prepare-network-prerequisites`. That prepare step in turn re-runs the join,
which fails again on the invalid key -> the observed rollout loop.

## Operator confirmation probes (run from hub/operator, read-only)

These are the exact non-mutating checks that establish the three claims. They
read state only; none of them rotate the key, mutate the mesh, or echo the key.

- Claim 1 (worker-3 reachable via bastion + toolchain + supervised TS 1.98.9):
  from the operator workstation, over the pinned bastion route the deploy
  tooling already uses (`ssh_target_args`/ProxyJump, host-key strict), run on
  the worker-3 node:
  `tailscale version` (expect client 1.98.9);
  `sudo -n supervisorctl status mac-tailscaled` (expect RUNNING; supervised
  daemon per `install-tailscale.sh` supervisord branch, socket
  `/run/tailscale/mac.sock`);
  `command -v codex-runner || ls ~/.mac` and OpenShell service status to confirm
  the MAC/OpenShell toolchain is live. Reaching the node at all over the pinned
  route confirms the authoritative bastion route.
- Claim 2 (hub route `<hub-mesh-ip>:8789` unreachable over tailscale0): on
  worker-3, `python3 -c "import socket; socket.create_connection(('<hub-mesh-ip>',8789),5)"`
  — expect it to raise (timeout/EHOSTUNREACH/ECONNREFUSED). This is exactly the
  probe `probe_remote_hub_tcp()` performs. Also `tailscale status --json` on
  worker-3 should show `BackendState` != `Running` (or no route to the hub node),
  confirming the missing mesh path rather than a hub-side outage.
- Claim 3 (current key rejected by control plane, class only): on worker-3
  inspect the supervised daemon's own record of the last join attempt WITHOUT
  re-running `up` with the key on the command line — read
  `tailscale status --json` (`BackendState`, `AuthURL`) and the tailscaled log
  (`journalctl`/supervisord `tailscaled.log`) for the control-plane verdict.
  Record only the CLASS: `expired`, `revoked`, `wrong-tailnet`/tagged-mismatch,
  or `key-not-reusable-already-used`. Do NOT capture or transcribe the key.
  (Note: because `install-tailscale.sh` suppresses `up` output, the class must
  come from the daemon/control-plane state, not from any deploy log line.)

## Preconditions the operator rotation MUST satisfy

The replacement `MAC_DEPLOY_TAILSCALE_AUTH_KEY` (delivered as the fleet-scoped
`MAC_DEPLOY_TAILSCALE_AUTH_KEY__<FLEET>` and/or the flat form, or stored in the
hub SecretsService vault) must be issued so that the automated,
non-interactive `tailscale up` in `install-tailscale.sh` succeeds:

- **Format**: `tskey-auth-...` (>= 8 chars after the prefix) so it passes
  `_looks_like_tailscale_auth_key()` and does not trip the setup warning.
- **Non-interactive enrollment**: **reusable** (worker-3 plus any re-run of the
  prepare step will consume it more than once) OR paired with a distinct
  single-use key per node — the join runs with no operator present.
- **Authority / tailnet**: issued in the **same tailnet the hub
  (`<hub-mesh-ip>`) lives in** so worker-3 lands on a `tailscale0` route to the
  hub. If the tailnet enforces ACL tags, the key must be a **tagged/`--advertise
  tags`-compatible auth key** whose tag is authorized to reach the hub's
  control port; otherwise the join is accepted but routing/ACLs still block the
  hub -> re-verify Claim 2 after join.
- **Not expired / not revoked**: expiry window comfortably longer than the
  rollout, and the key not previously revoked.
- **Not ephemeral unless intended**: if `--ephemeral` is used the node is GC'd
  when offline; for a persistent worker-3 use a non-ephemeral key.
- **Delivery**: place the value only in the resolved credential source
  (scoped env var, flat env var, or hub vault via SecretsService) — never in
  `deploy/fleet/config.yaml` or any Git-tracked file.

## Acceptance checks for later phases (post-rotation)

Later phases may declare worker-3 rolled out only when ALL of these pass, in
order, with no key value logged:

1. **Credential resolvable, format-valid**: `fleet_scoped_env
   MAC_DEPLOY_TAILSCALE_AUTH_KEY <fleet>` yields a non-empty single-line value
   and `mac`/`fleet_setup` render emits no "does not look like a valid tskey-"
   warning and no missing-`required_env` error. (Presence/format only — do not
   print the value.)
2. **Mesh join succeeds on worker-3**: after `--prepare-network-prerequisites`,
   `tailscale status --json` shows `BackendState == "Running"` and worker-3 has a
   Tailscale IP (the `wait_for_tailscale_ip` success condition), with no
   `AuthURL`/needs-login state.
3. **Hub route reachable**: `probe_remote_hub_tcp` for worker-3 succeeds, i.e.
   TCP connect to `<hub-mesh-ip>:8789` over `tailscale0` completes, and
   `classify_network_prerequisites()` reports "hub route prerequisite ready".
4. **Deploy proceeds past network gate**: `prepare_network_prerequisites()`
   re-prove step exits 0 (no "refusing every network mutation" and no
   "unreachable" cutover error), confirming the blocker is cleared.
5. **No secret leakage regression**: deploy logs still show only the suppressed
   generic failure text on any failure path (no auth-bearing suggested command),
   preserving the `>/dev/null 2>&1` guard in `install-tailscale.sh`.

## Status: CONFIRMED-BY-CODE-PATH; live re-measurement deferred to hub

The blocker mechanism is fully accounted for in source: an invalid (well-formed
but expired/revoked/wrong-tailnet/non-reusable) `MAC_DEPLOY_TAILSCALE_AUTH_KEY`
passes every checked-in format/presence gate but is rejected by the Tailscale
control plane at the suppressed-output `tailscale up` in `install-tailscale.sh`,
leaving worker-3 off the mesh and the hub route `<hub-mesh-ip>:8789` unreachable,
which the deploy network gate then refuses. The three runtime claims must be
confirmed with the read-only operator probes above from the hub/operator
context (this sandbox cannot reach the mesh, the node, or the control plane, and
holds no key material). No network or secret state was mutated and no key value
was placed, echoed, or logged by this assessment.
