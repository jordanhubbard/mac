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

> **macOS:** OpenShell's kernel primitives (Landlock, seccomp, network
> namespaces) run inside the **Docker Desktop Linux VM**, not on the host. You
> need Docker Desktop running. On Linux hosts they run natively (kernel ≥ 5.13
> for Landlock).

```bash
curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
# or: uv tool install -U openshell
openshell status        # gateway must be reachable
```

## Enable

```bash
cp deploy/openshell/mac-hermes-policy.yaml /etc/mac/openshell-policy.yaml
$EDITOR /etc/mac/openshell-policy.yaml      # fill in __PLACEHOLDER__ tokens

export MAC_OPENSHELL_POLICY=/etc/mac/openshell-policy.yaml
export MAC_OPENSHELL_REQUIRED=1
export MAC_ALLOW_UNSANDBOXED_YOLO=0
mac-openshell-supervisor --agent-id agent_rocky --policy "$MAC_OPENSHELL_POLICY" -- mac-hermes-gateway
```

### Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAC_OPENSHELL_REQUIRED` | hub/worker-1/worker-2 required | fail closed when OpenShell/policy is unavailable |
| `MAC_OPENSHELL_BIN` | `openshell` | path to the `openshell` binary |
| `MAC_OPENSHELL_POLICY` | _(resolved)_ | explicit policy path; MAC-managed materialized policy should be set here |
| `MAC_OPENSHELL_EVENTS_FILE` | _(none)_ | JSONL/OCSF event stream for `mac-openshell-collector` |
| `MAC_ALLOW_UNSANDBOXED_YOLO` | `0` on required agents | explicit hatch for non-required hosts |
| `MAC_OPENSHELL_SANDBOX` | _(off)_ | deprecated compatibility: one-shot task executor wrapping |
| `MAC_OPENSHELL_SANDBOX_NAME` | _(ephemeral)_ | fixed sandbox name (debug only) |
| `MAC_OPENSHELL_KEEP` | _(off)_ | truthy → `--keep` (don't tear down; debug) |
| `MAC_OPENSHELL_CREATE_ARGS` | _(none)_ | extra `sandbox create` args (shell-split), e.g. `--from img`, `--upload /src:/src` |
| `MAC_OPENSHELL_ENV_PASSTHROUGH` | hub+gateway vars | comma list of env names forwarded via `--env` |

## Verify (requires Docker + OpenShell installed)

1. `openshell status` healthy.
2. Dry-run the wrap without spawning:
   ```python
   import os; os.environ["MAC_OPENSHELL_SANDBOX"]="1"
   os.environ["MAC_OPENSHELL_POLICY"]="/etc/mac/openshell-policy.yaml"
   from mac import task_executor as te
   print(te._maybe_wrap_openshell(te._hermes_argv("hello")))
   ```
   Confirm it begins with `openshell sandbox create … --policy … --` and ends
   with the Hermes argv.
3. Start `mac-openshell-supervisor` on hub, worker-1, and worker-2. Confirm
   the gateway, task executor, finalizers, and Hermes sessions inherit the same
   sandbox id.
4. Trigger an off-policy filesystem or network attempt. Confirm the denial
   appears in OpenShell logs, `/action-events`, the dashboard Observability
   action feed, memory summary eligibility, and OTLP export.

## Follow-up

- Build a sandbox image (or `--upload` payload) containing the Hermes runtime
  (`~/.mac/venv` + `~/.mac/src`) so `--from` works on fleet hosts.
- Tune the operator policy against the real model-gateway/hub hosts (both the
  bundled default and the operator template already use
  `landlock: hard_requirement`).
- Replace any remaining production use of `MAC_OPENSHELL_SANDBOX` /
  `MAC_OPENSHELL_GATEWAY` with the supervisor unit.
