# Running Hermes under the OpenShell sandbox

This describes MAC's OpenShell runtime path: one per-agent OpenShell supervisor
launches Hermes gateway/session work and autonomous executor children as
confined descendants of an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox. OpenShell is the **sole guardrail authority**, letting the agent run
full `--yolo` safely.

## Why

The executor already launches Hermes with `--yolo` (Hermes' own approval prompts
bypassed — see `_hermes_argv` in `src/mac/task_executor.py`). On its own that is
**unguarded**. Wrapping the process in OpenShell makes YOLO *safe* by enforcing
every guardrail from a declarative policy:

| Concern | Enforced by | How |
| --- | --- | --- |
| Filesystem | OpenShell Landlock | allow-list of read-only / read-write paths |
| Syscalls / privilege | OpenShell seccomp | syscall filter; never runs as root |
| Network egress | OpenShell L7 proxy | **deny-by-default**; per-host/per-binary rules |

One guardrail system (the OpenShell policy YAML), not two.

## What this adds

The production path is `mac-openshell-supervisor`. It starts a named sandbox
for the agent, passes the MAC-assigned policy, disables unsandboxed YOLO, and
runs `mac-hermes-gateway` or the configured child process inside the sandbox.
The legacy `_maybe_wrap_openshell()` task-executor seam and
`MAC_OPENSHELL_GATEWAY` gateway re-exec path remain as compatibility knobs.

**A policy is always passed — enabling can never silently fall back to
OpenShell's image-default profile.** `_resolve_openshell_policy()` resolves `<P>`
in this order, and raises if none is found (fail closed). A MAC-managed policy
assigned to the agent should be materialized to `MAC_OPENSHELL_POLICY` by
deployment; that wins over file fallback.

1. `MAC_OPENSHELL_POLICY` (explicit) — must exist, else error.
2. `~/.mac/openshell-policy.yaml` — the operator-filled fleet policy.
3. the bundled fail-closed default `src/mac/openshell/default-policy.yaml`.

The **bundled default is a lockdown**: filesystem-confined, never root,
`landlock: hard_requirement`, and **all network egress denied** (empty
`network_policies`). Under it the agent can't reach the hub/gateway, so an
unconfigured deployment fails closed rather than running under an unknown
profile. For real use, copy the **operator template**
`deploy/openshell/mac-hermes-policy.yaml` (which allows the hub, model gateway,
and GitHub, and also defaults to `landlock: hard_requirement`), fill in the
`__PLACEHOLDER__` hosts, and install it at `~/.mac/openshell-policy.yaml` or
point `MAC_OPENSHELL_POLICY` at it.

OpenShell OCSF/event output is normalized by `mac-openshell-collector` into
`mac.action_event.v1` records and posted to `/action-events`. The legacy
`/events`, `/command-audit`, and `/observability` surfaces remain readable;
the action ledger is the canonical normalized stream and can export an
OTLP-compatible shape through `/action-events/export/otlp`.

## Prerequisites

MAC/OpenShell uses one container runtime: **Docker Engine/Moby through
OpenShell's Docker driver**. This is the production contract for bare metal,
VMs, and containerized environments that support nested Docker/DinD. Do not use
Docker Desktop, Podman, or `podman-docker` for fleet nodes; those create
different image stores, gateway configs, GPU behavior, and failure modes.

On Linux hosts, OpenShell's kernel primitives run natively (kernel ≥ 5.13 for
Landlock). On non-Linux developer machines, run a Linux VM/container with OSS
Docker Engine/Moby and validate there; the production architecture does not
depend on Docker Desktop licensing or behavior.

OpenShell 0.0.62 had a runtime-driver mismatch on some Linux hosts: the gateway
could be configured with only `[openshell.drivers.docker]` while still logging
`openshell_driver_podman` and reading the sandbox image from the user's Podman
image store. This mismatch is resolved in OpenShell 0.0.72 (the current fleet
pin). `bootstrap-openshell.sh` retains the `mirror_image_for_openshell_runtime`
step as belt-and-suspenders, and still runs an `openshell sandbox create` smoke
test that verifies `gh`, `codex`, and `codegraph` are visible. Bootstrap then
runs `live-confinement-probe.sh` inside a second throwaway sandbox and fails
closed unless the runtime proves the expected filesystem, egress, privilege,
seccomp, user-namespace, and raw-socket boundaries.

```console
deploy/openshell/bootstrap-openshell.sh --enable --fail-closed
docker info             # must be a real Docker Engine/Moby daemon, not Podman
openshell gateway list  # gateway must be reachable
```

After host validation, reconcile the hub's OpenShell ledger. Bootstrap success
does not by itself prove the hub knows the agent is required, which policy is
assigned, or whether the runtime is currently deployed. The reconciliation
command reads the enabled Linux agents from `~/.mac/fleets.yaml` unless
`--agent` is passed explicitly, defaults to dry-run, and preserves existing
agent resources while setting only `resources.openshell_required`.

```console
mac openshell reconcile --target-fleet <fleet>
mac openshell reconcile --target-fleet <fleet> --apply --validated \
  --sandbox-id docker-openshell-smoke-$(date +%Y%m%d) \
  --validation-summary "Docker image smoke and OpenShell sandbox smoke passed"
mac openshell status --agent agent_hub
```

`--validated` is required when applying `status=active`; failed or degraded
hosts should still be reconciled as required, but reported with
`--status failed` or `--status degraded` so `effective.fail_closed` remains
truthful.

## Enable

```console
cp deploy/openshell/mac-hermes-policy.yaml /etc/mac/openshell-policy.yaml
$EDITOR /etc/mac/openshell-policy.yaml      # fill in __PLACEHOLDER__ tokens

export MAC_OPENSHELL_POLICY=/etc/mac/openshell-policy.yaml
export MAC_ALLOW_UNSANDBOXED_YOLO=0
mac-openshell-supervisor --agent-id agent_hub --policy "$MAC_OPENSHELL_POLICY" -- mac-hermes-gateway
```

### Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAC_OPENSHELL_REQUIRED` | derived from `agent.resources.openshell_required` | fail closed when OpenShell/policy is unavailable |
| `MAC_OPENSHELL_BIN` | `openshell` | path to the `openshell` binary |
| `MAC_OPENSHELL_POLICY` | _(resolved)_ | explicit policy path; MAC-managed materialized policy should be set here |
| `MAC_OPENSHELL_EVENTS_FILE` | _(none)_ | JSONL/OCSF event stream for `mac-openshell-collector` |
| `MAC_ALLOW_UNSANDBOXED_YOLO` | `0` on required agents | explicit hatch for non-required hosts |
| `MAC_OPENSHELL_SANDBOX` | _(off)_ | deprecated compatibility: one-shot task executor wrapping |
| `MAC_OPENSHELL_SANDBOX_NAME` | _(ephemeral)_ | fixed sandbox name (debug only) |
| `MAC_OPENSHELL_KEEP` | _(off)_ | truthy → `--keep` (don't tear down; debug) |
| `MAC_OPENSHELL_GC` | set to `1` by bootstrap | reconcile old orphaned MAC-owned sandboxes before new executor or hub-verification work |
| `MAC_OPENSHELL_STALE_AFTER_SECONDS` | `86400` | minimum age for automatic sandbox garbage collection |
| `MAC_OPENSHELL_CREATE_ARGS` | _(none)_ | extra `sandbox create` args (shell-split), e.g. `--from img`, `--upload /src:/src` |
| `MAC_OPENSHELL_ENV_PASSTHROUGH` | hub+gateway vars | comma list of env names forwarded through the private sandbox environment file |

## Verify (requires Docker Engine/Moby + OpenShell installed)

1. `docker info` succeeds and `docker --version` is not a Podman compatibility
   shim.
2. `openshell gateway list` shows the selected gateway.
3. `~/.mac/openshell/live-confinement-probe.log` ends with
   `CONFINEMENT_PROBE_OK`; bootstrap will not enable enforcement without it.
4. Inspect orphan cleanup before applying it manually:
   ```console
   mac openshell sandbox-gc
   mac openshell sandbox-gc --apply
   ```
   The default 24-hour grace period protects recent work. New sandboxes are
   labeled with their MAC owner, lifecycle kind, creator PID, and debug-keep
   status; a live creator or `mac.keep=true` is never collected. Exact legacy
   `mac-task-*` and `mac-hubverify-*` names remain eligible after the grace
   period so pre-label leaks can be retired.
5. Dry-run the wrap without spawning:
   ```python
   import os; os.environ["MAC_OPENSHELL_SANDBOX"]="1"
   os.environ["MAC_OPENSHELL_POLICY"]="/etc/mac/openshell-policy.yaml"
   from mac import task_executor as te
   print(te._maybe_wrap_openshell(te._hermes_argv("hello")))
   ```
   Confirm it begins with `openshell sandbox create … --policy … --` and ends
   with the Hermes argv.
6. Start `mac-openshell-supervisor` on hub, worker-1, and worker-2. Confirm
   the gateway, task executor, finalizers, and Hermes sessions inherit the same
   sandbox id.
7. Trigger an off-policy filesystem or network attempt. Confirm the denial
   appears in OpenShell logs, `/action-events`, the dashboard Observability
   action feed, memory summary eligibility, and OTLP export.

## Coding-agent CLIs in the sandbox

The executor prefers an installed, authenticated coding-agent CLI (Claude Code,
Codex, Cursor) over a direct LLM-gateway run, because those CLIs authenticate
against a subscription/seat instead of a metered API token (see
`src/mac/coding_agent.py`). A coding-agent run is launched exactly like the
Hermes run — inside the same OpenShell sandbox, under the same policy — so the
guardrails are unchanged.

**Working outside the sandbox is not sufficient to enable a coding CLI.** When
OpenShell confinement is in effect (the per-task wrap *or* the supervisor — i.e.
`MAC_OPENSHELL_REQUIRED` truthy / the agent is required), coding-agent
enablement is **gated on a real in-sandbox preflight**: a throwaway sandbox runs
the CLI under the live policy + forwarded env and must echo a sentinel back,
proving end-to-end that the **binary exists, credentials resolve, and egress to
the provider is permitted** in the sandbox.

The worker runs this probe before dispatch and publishes a secret-free
`mac.coding_clis.v2` heartbeat record containing the CLI, provider, wire
protocol, endpoint, authentication kind/source, model, route fingerprint, and
the matching `mac.coding_agent.verification.v1` result. Repository dispatch to
an OpenShell agent requires a fresh successful proof. A task-pinned model also
requires proof for that exact model. Presence of a binary, credential variable,
or credential directory is only `configured`; it is never `verified`.

A failed probe carries a `failure_class` in that
`mac.coding_agent.verification.v1` record so the failure names its own repair.
Two of those classes are easy to confuse because the sandbox proxy denies
egress with the same HTTP status a provider uses to reject a credential:

| `failure_class` | Meaning | Repair |
| --- | --- | --- |
| `sandbox_policy_denied` | The OpenShell egress policy refused the destination — the response carries a policy sentinel such as `policy_denied` or `not permitted by policy`. | Allow the endpoint in the sandbox policy / fix the route. The credential is untouched. |
| `authentication_failed` | A genuine `401`/`403` (or an explicit invalid-key message) from the provider, with no policy sentinel. | Repair the credential (`mac creds-sync`). |

Because a policy denial proves the CLI launched and opened a socket, its
`binary_status` is `present`, and it is deliberately *not* reported as a CLI
needing a credential sync.

The executor repeats the same fail-closed check after claim. If the selected
CLI fails and the progress observer proves that the sandbox is clean and has no
evidence manifest, repository bootstrap/tests/CodeGraph/publication are skipped;
harvest and teardown still run. This makes an unavailable route terminate
promptly without disguising it as a long finalizer stall.

The unattended finalizer auto-commits modified tracked files but deliberately
refuses untracked or staged-new files. A coding agent must commit its own new
files before finishing. If an otherwise successful executor run is preserved
with passing contract-test and CodeGraph evidence but is refused only at this
boundary, inspect it without mutation first:

```console
mac task recover-finalizer /path/to/task-workspace --json
```

Recovery is explicit and allow-listed. Repeat `--approve-new-file PATH` for
every intended new path, provide the original executor `--evidence-id`, and add
`--execute`. MAC validates the preserved HEAD and evidence, commits with
provenance, rebases, reruns both gates, and uses the shared guarded push. It
never invokes the executor/model again and does not weaken ordinary unattended
finalization.

Preserved test evidence is judged on gate semantics, not argv spelling. The
contract command counts, and so does the repository's own sanity wrapper
(`scripts/run-sanity-tests.sh --base <prepared base sha>`) that the executor
sandbox uses to scope the same gate — but only when the wrapper is committed in
the preserved HEAD, its `--base` names this task's prepared base, it carries no
argument outside `--base`/`--changed-file`, and it passed. Anything else (an
arbitrary command, a stale or missing base, a nonzero result) is still refused,
and the accepted item is echoed as `preserved_test_evidence` in the plan.
Whichever spelling was preserved, recovery reruns the full contract command
itself after rebasing onto canonical.

A separate failure mode is a finalizer that harvested verified work but was
itself interrupted — a timeout, cancellation, or crash after the contract-test
and CodeGraph gates passed but before the guarded push confirmed a remote ref.
The interrupted run leaves a partial `mac-evidence.json` (with a
`finalizer_interrupted` marker) and a `finalizer-progress.json` stuck in a
non-terminal status. Resume it the same way:

```console
mac task recover-stalled-finalizer /path/to/task-workspace --json
```

Add `--approve-new-file PATH` only for any new files the stalled finalizer left
uncommitted, provide the original `--evidence-id`, and add `--execute`. MAC
revalidates the preserved HEAD, commits any pending work with stalled-finalizer
provenance, rebases, reruns both gates, and performs the shared guarded push.

For a coding agent to pass the preflight, the deployment must ensure, **inside
the sandbox**:

1. **Binary present** — `claude` / `codex` / `cursor-agent` is in the sandbox
   image (or uploaded via `MAC_OPENSHELL_CREATE_ARGS`). The standard MAC image
   installs `codex`.
2. **Credentials reachable and durable** — supported environment routes
   (`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `CLAUDE_CODE_OAUTH_TOKEN`,
   `CURSOR_AUTH_TOKEN`, `CURSOR_API_KEY`, `MAC_CODEX_TOKEN`, `CODEX_API_KEY`,
   `OPENAI_API_KEY`) are
   forwarded automatically. File-based Codex OAuth state
   (`~/.codex/auth.json`) is not uploaded by default because OpenShell upload is
   copy-only: a throwaway sandbox can consume and rotate the refresh token while
   the replacement is lost with the sandbox. `bootstrap-openshell.sh` only
   uploads Codex file auth when `MAC_OPENSHELL_UPLOAD_CODEX_AUTH=1`, and the
   executor only retains and probes that upload when
   `MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH=1` is also still set. A stale rendered
   upload is removed at execution time, and `OPENAI_API_KEY` always suppresses
   the file copy because environment auth wins.
3. **Baseline repo tools present** — the MAC OpenShell image installs `git`,
   `gh`, and `codegraph`; custom images must provide the same baseline if they
   are used for repository work.
4. **Egress allowed** — the OpenShell policy's `network_policies` must permit the
   hub/gateway, provider host (e.g. `api.anthropic.com`), git host, and Python
   package index hosts used by repository bootstrap (`pypi.org` and
   `files.pythonhosted.org` in the standard policy). The bundled fail-closed
   default denies all egress, so the preflight fails closed there by design.

### Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAC_PREFER_CODING_AGENT` | `1` | master switch; `0` always uses the Hermes → gateway path |
| `MAC_CODING_AGENT` | _(auto)_ | pin to `claude`/`codex`/`cursor`, or `off` to disable |
| `MAC_CODING_AGENT_SANDBOX` | `verify` | `verify` = gate on the in-sandbox preflight; `trust` = assume the image is provisioned (skip the probe); `off` = never use a coding agent when confined |
| `MAC_CODING_AGENT_PREFLIGHT_TIMEOUT` | `180` | seconds for the in-sandbox preflight |
| `MAC_CODING_AGENT_PREFLIGHT_TTL_SECONDS` | `900` | successful route-proof lifetime in the executor/worker process |
| `MAC_CODING_AGENT_PREFLIGHT_FAILURE_TTL_SECONDS` | `60` | retry interval for a failed route proof |
| `MAC_WORKER_CODING_ROUTE_PROBE_INTERVAL_SECONDS` | success `900`, failure `60` | worker heartbeat probe cadence |
| `MAC_CODING_ROUTE_MAX_AGE_SECONDS` | `1200` | maximum proof age accepted by dispatch |
| `MAC_CODING_AGENT_<AGENT>_CMD` | _(built-in)_ | override a CLI's invocation (shlex-split); prompt appended as the trailing arg |
| `MAC_CODING_AGENT_MESSAGING_MCP` | `1` | register the messaging MCP server (unconfined path only, Claude) |
| `MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT` | `1` in fleet deploy | executor strict mode: repository tasks fail closed unless a coding CLI is verified in-sandbox |
| `MAC_OPENSHELL_UPLOAD_CODEX_AUTH` | `0` | bootstrap opt-in to upload `~/.codex/auth.json` / `config.toml` into sandboxes |
| `MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH` | `0` | executor opt-in to probe/use uploaded Codex file auth despite refresh-token rotation risk |
| `MAC_CODEX_BASE_URL` | `OPENAI_BASE_URL` | explicit Codex provider endpoint; use a Responses-compatible endpoint |
| `MAC_CODEX_TOKEN` | _(unset)_ | bearer read by Codex through `env_key`; the value never appears in argv or telemetry |
| `MAC_CODEX_PROVIDER` | inferred | secret-free custom provider id (`openai` for the built-in endpoint, otherwise `mac-router`) |
| `MAC_CODEX_WIRE_API` | `responses` | Codex wire protocol recorded and rendered into per-run custom-provider config |
| `MAC_CODEX_MODEL` | fleet/task default | model included in the route fingerprint and dispatch proof |

Set `MAC_CODING_AGENT_SANDBOX=trust` only after validating the image+policy out
of band; it skips the per-task proof. `python -m mac.coding_agent` prints the
(secret-free) host-side routing decision for the current environment.

## NemoClaw single-host pilot observations

Status: pilot observations recorded 2026-07-04. These notes are intentionally
fleet-generic: use role names, placeholder workspace/channel identifiers, and
redacted credential sources in downstream runbooks; do not record real
usernames, hostnames, tokens, Slack team names, or local fleet identities.

### Slack multi-account behavior

- Treat each Slack Socket Mode app token as single-owner for the pilot. Running
  Hermes and NemoClaw against the same app token can create competing event
  consumers, duplicate replies, or ambiguous thread ownership.
- A single Hermes gateway can use the existing multi-account file shape for
  multiple workspaces; that does not coordinate an additional external
  NemoClaw gateway. If both gateways must be live, give each gateway its own
  Slack app credentials and make the intended workspace/channel binding
  explicit.
- Home-channel fan-out remains per connected workspace. Pilot notes should name
  routes with placeholders such as `<workspace-id>/<channel-id>` and should
  avoid storing raw tokens or workspace-local display names.

### OpenShell policy friction

- MAC's default OpenShell posture is deny-by-default egress plus explicit
  filesystem allow-lists. NemoClaw adds non-MAC traffic patterns, including
  Slack Web API calls, Socket Mode WebSocket egress, provider endpoints, and any
  package or model download endpoints used at startup. Those must be policy
  entries, not interactive approvals.
- The policy iteration loop is the main operational cost: every denied
  endpoint or missing writable path should become a small, reviewed policy
  change followed by an off-policy-denial smoke test. Do not loosen the policy
  to broad internet or broad home-directory access just to make the pilot pass.
- Keep MAC-owned evidence, task worktrees, and finalization outside NemoClaw's
  writable state. The sandboxed gateway may write its runtime cache/log paths,
  but repository publication remains the deterministic MAC finalizer's job.

### OpenShell 0.0.72 compatibility — validated 2026-07-04

OpenShell 0.0.72 has been validated against all three MAC sandbox surfaces
(executor sandbox create, hub-verify tar-upload verify, gateway confinement).
The fleet pin was advanced from 0.0.62 to 0.0.72 in bootstrap-openshell.sh,
openshell_reconcile.py, and cli.py. The existing mac-hermes-policy.yaml
template is fully forward-compatible; no policy adjustments are needed.

Key behavior changes in 0.0.72 relative to 0.0.62:
- Native messaging credential rewrite (opt-in via `credential_rewrite` policy
  key; MAC policy has no such key — behavior unchanged).
- WebSocket text-frame and REST body L7 enforcement (tightens `access: read-only`
  endpoints to also block upload POST bodies; MAC's python_packages and
  node_packages blocks now enforce this correctly).
- MCP/JSON-RPC enforcement (opt-in per endpoint; MAC policy uses `protocol: rest`
  throughout — no change).
- `policy get --base` CLI subcommand (informational; MAC uses per-sandbox
  `--policy` injection, not base-policy inheritance — no change).

See docs/security/openshell-0.0.72-compatibility-review.mdx for the full
pass/fail surface results, behavior-change table, and recommendation narrative.

The 0.0.62 podman-driver mismatch workaround (`mirror_image_for_openshell_runtime`)
is retained in bootstrap-openshell.sh as belt-and-suspenders; the upstream
mismatch is resolved in 0.0.72 but the mirror step is harmless when Podman
is absent.

### Fallback decision

If NemoClaw's OpenShell pin is disruptive, fall back to raw `openclaw` confined
by a MAC-authored OpenShell policy on the last validated MAC/OpenShell pin
(currently 0.0.72; roll back to 0.0.62 only if a 0.0.72-specific failure is
confirmed):

1. Build or upload a sandbox image containing raw `openclaw`, the required MAC
   baseline tools (`git`, `gh`, `codegraph`), and only the runtime dependencies
   needed by the pilot.
2. Start from `deploy/openshell/mac-hermes-policy.yaml`, then add explicit
   Slack, model-provider, hub/gateway, Git host, and package-index egress rules
   required by raw `openclaw`. Keep filesystem writes scoped to the task
   workspace and gateway runtime cache/log directories.
3. Use separate Slack app credentials from any Hermes gateway that remains
   online. Route pilot traffic through placeholder-documented
   `<workspace-id>/<channel-id>` bindings.
4. Verify with `openshell gateway list`, an `openshell sandbox create` smoke
   test, one Slack receive/send loop, one intentional off-policy denial, and a
   normal MAC repository task whose evidence is finalized by MAC.
5. Treat this as a temporary compatibility bridge. Promote NemoClaw only after
   its OpenShell version, policy shape, and event stream pass the same MAC
   smoke tests.

### Operational gaps

- Add a version matrix covering NemoClaw, raw `openclaw`, OpenShell, Docker
  Engine/Moby, the sandbox image digest, and the active MAC policy revision.
- Define ownership for Slack app isolation, policy review, event-log retention,
  and rollback. The rollback condition should be simple: loss of sandbox
  enforcement, ambiguous Slack routing, missing evidence, or unreviewed broad
  egress returns the host to the last validated MAC/OpenShell 0.0.72 path.
- Add a short pilot checklist that records only secret-free facts: credential
  source names, placeholder route identifiers, policy revision, image digest,
  smoke-test outcome, and finalizer evidence status.

## Follow-up

- Tune the operator policy against the real model-gateway/hub hosts (both the
  bundled default and the operator template already use
  `landlock: hard_requirement`).
- Replace any remaining production use of `MAC_OPENSHELL_SANDBOX` /
  `MAC_OPENSHELL_GATEWAY` with the supervisor unit.
