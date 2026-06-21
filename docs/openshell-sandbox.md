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

OpenShell 0.0.62 has a runtime-driver mismatch on some Linux hosts: the gateway
can be configured with only `[openshell.drivers.docker]` while still logging
`openshell_driver_podman` and reading the sandbox image from the user's Podman
image store. `bootstrap-openshell.sh` handles this outside the agents by
mirroring the Docker-built image into the runtime-visible store, then running an
`openshell sandbox create` smoke test that verifies `gh`, `codex`, and
`codegraph` are visible before the node is considered ready.

```bash
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

```bash
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

```bash
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
| `MAC_OPENSHELL_CREATE_ARGS` | _(none)_ | extra `sandbox create` args (shell-split), e.g. `--from img`, `--upload /src:/src` |
| `MAC_OPENSHELL_ENV_PASSTHROUGH` | hub+gateway vars | comma list of env names forwarded via `--env` |

## Verify (requires Docker Engine/Moby + OpenShell installed)

1. `docker info` succeeds and `docker --version` is not a Podman compatibility
   shim.
2. `openshell gateway list` shows the selected gateway.
3. Dry-run the wrap without spawning:
   ```python
   import os; os.environ["MAC_OPENSHELL_SANDBOX"]="1"
   os.environ["MAC_OPENSHELL_POLICY"]="/etc/mac/openshell-policy.yaml"
   from mac import task_executor as te
   print(te._maybe_wrap_openshell(te._hermes_argv("hello")))
   ```
   Confirm it begins with `openshell sandbox create … --policy … --` and ends
   with the Hermes argv.
4. Start `mac-openshell-supervisor` on hub, worker-1, and worker-2. Confirm
   the gateway, task executor, finalizers, and Hermes sessions inherit the same
   sandbox id.
5. Trigger an off-policy filesystem or network attempt. Confirm the denial
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

If the preflight fails, or if the selected CLI uses non-durable sandbox
credentials, tasks fall back to the in-image Hermes runtime. Hermes still runs
inside OpenShell against the uploaded worktree; the deterministic finalizer is
the evidence gate and rejects repository runs that leave no changed files,
passing tests/checks, or evidence manifest. Operators may restore the older
fail-closed behavior with `MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT=1` after
they provision durable in-sandbox coding-agent credentials. The preflight
verdict is cached per worker process.

For a coding agent to pass the preflight, the deployment must ensure, **inside
the sandbox**:

1. **Binary present** — `claude` / `codex` / `cursor-agent` is in the sandbox
   image (or uploaded via `MAC_OPENSHELL_CREATE_ARGS`). The standard MAC image
   installs `codex`.
2. **Credentials reachable and durable** — env-key auth (`ANTHROPIC_API_KEY`,
   `CURSOR_API_KEY`) is forwarded automatically. File-based Codex OAuth state
   (`~/.codex/auth.json`) is not uploaded by default because OpenShell upload is
   copy-only: a throwaway sandbox can consume and rotate the refresh token while
   the replacement is lost with the sandbox. `bootstrap-openshell.sh` only
   uploads Codex file auth when `MAC_OPENSHELL_UPLOAD_CODEX_AUTH=1`, and the
   executor only probes that auth when `MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH=1`.
3. **Baseline repo tools present** — the MAC OpenShell image installs `git`,
   `gh`, and `codegraph`; custom images must provide the same baseline if they
   are used for repository work.
4. **Egress allowed** — the OpenShell policy's `network_policies` must permit the
   provider host (e.g. `api.anthropic.com`) in addition to the hub/gateway. The
   bundled fail-closed default denies all egress, so the preflight fails closed
   there by design.

### Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAC_PREFER_CODING_AGENT` | `1` | master switch; `0` always uses the Hermes → gateway path |
| `MAC_CODING_AGENT` | _(auto)_ | pin to `claude`/`codex`/`cursor`, or `off` to disable |
| `MAC_CODING_AGENT_SANDBOX` | `verify` | `verify` = gate on the in-sandbox preflight; `trust` = assume the image is provisioned (skip the probe); `off` = never use a coding agent when confined |
| `MAC_CODING_AGENT_PREFLIGHT_TIMEOUT` | `180` | seconds for the in-sandbox preflight |
| `MAC_CODING_AGENT_<AGENT>_CMD` | _(built-in)_ | override a CLI's invocation (shlex-split); prompt appended as the trailing arg |
| `MAC_CODING_AGENT_MESSAGING_MCP` | `1` | register the messaging MCP server (unconfined path only, Claude) |
| `MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT` | `0` | strict mode: repository tasks fail closed unless a coding CLI is verified in-sandbox |
| `MAC_OPENSHELL_UPLOAD_CODEX_AUTH` | `0` | bootstrap opt-in to upload `~/.codex/auth.json` / `config.toml` into sandboxes |
| `MAC_OPENSHELL_ALLOW_CODEX_FILE_AUTH` | `0` | executor opt-in to probe/use uploaded Codex file auth despite refresh-token rotation risk |

Set `MAC_CODING_AGENT_SANDBOX=trust` only after validating the image+policy out
of band; it skips the per-task proof. `python -m mac.coding_agent` prints the
(secret-free) host-side routing decision for the current environment.

## Follow-up

- Tune the operator policy against the real model-gateway/hub hosts (both the
  bundled default and the operator template already use
  `landlock: hard_requirement`).
- Replace any remaining production use of `MAC_OPENSHELL_SANDBOX` /
  `MAC_OPENSHELL_GATEWAY` with the supervisor unit.
