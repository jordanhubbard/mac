# OpenShell + NeMo Relay integration

This document describes how the MAC fleet runs the Hermes agent under the
**OpenShell** sandbox (the sole guardrail authority) with **NeMo Relay** as the
observability runtime — the plan, what is implemented today, and how to turn it
on and verify it.

## Why

The task executor already launches Hermes with `--yolo` (Hermes' own
permission/approval prompts are bypassed — see `_hermes_argv` in
`src/mac/task_executor.py`). On its own that is **unguarded**. The goal of this
work is to make YOLO *safe* by confining the agent inside OpenShell, which then
enforces every guardrail from a declarative policy:

| Concern | Enforced by | How |
| --- | --- | --- |
| Filesystem | OpenShell Landlock | allow-list of read-only / read-write paths |
| Syscalls / privilege | OpenShell seccomp | syscall filter; never runs as root |
| Network egress | OpenShell L7 proxy | **deny-by-default**; per-host/per-binary rules |
| LLM credentials | OpenShell inference router | strip/inject provider creds (optional) |

So instead of two competing guardrail systems (Hermes' approval prompts +
OpenShell policy), there is exactly one: **the OpenShell policy YAML.**

## Architecture

```
mac worker ──► task_executor.main()
                  │  builds [python -m hermes_cli.main chat … --yolo]
                  ▼
        _maybe_wrap_openshell(argv)            (src/mac/task_executor.py)
                  │  if MAC_OPENSHELL_SANDBOX:
                  ▼
   openshell sandbox create --policy P --no-auto-providers --env … -- <hermes argv>
                  │                                   │
        confined Hermes process              OCSF event stream (/var/log/openshell-ocsf.*.log)
        (FS / syscalls / egress                       │
         enforced by policy P)                         ▼
                                          relay_observability.parse_ocsf_lines()
                                          (src/mac/relay_observability.py)
                                                       │  -> mac observations (layer="sandbox")
                                                       ▼
                                          ObservabilityService  +  NeMo Relay export
                                                       (SQLite sink kept; OTLP/ATIF/OI added)
```

**Layering choice.** OpenShell wraps the *entire* Hermes process (not just the
shell/code tools), so the agent's own filesystem and network actions are
confined too — not only the commands it shells out. This is the faithful
realization of "OpenShell provides ALL guardrails." (An alternative finer-grained
approach — adding an `openshell` backend to Hermes' `tools/environments/`
abstraction so only `terminal`/`execute_code`/`process` go through the sandbox —
is tracked as follow-up; it leaves Hermes' own I/O unconfined and so is a weaker
guarantee under YOLO.)

## What is implemented today (default OFF)

All of this is inert until `MAC_OPENSHELL_SANDBOX` is truthy — with it unset the
executor behaves exactly as before (proven by `tests/test_openshell_sandbox.py`).

1. **`_maybe_wrap_openshell()`** in `src/mac/task_executor.py` — a pure argv
   transform applied at the launch seam. Wraps the Hermes invocation in
   `openshell sandbox create … -- <argv>`.
2. **`src/mac/relay_observability.py`** — an import-guarded NeMo Relay adapter
   (no-op when `nemo_relay` is absent) plus a pure OpenShell **OCSF → mac
   observation** translator (`ocsf_to_observation`, `parse_ocsf_lines`).
3. **`deploy/openshell/mac-hermes-policy.yaml`** — the default guardrail policy
   template (filesystem allow-list, `run_as_user: sandbox`, deny-by-default
   network with rules for the MAC hub, model gateway, and GitHub).
4. Unit tests for both modules; full contract suite passes.

## How to enable

> **Prerequisite (macOS):** OpenShell's kernel primitives (Landlock, seccomp,
> network namespaces) run inside the **Docker Desktop Linux VM** on macOS, not
> on the host. You must have **Docker Desktop running** and a compute driver
> available. On Linux hosts they run natively (kernel ≥ 5.13 for Landlock).

1. Install OpenShell and start its gateway:
   ```bash
   curl -LsSf https://raw.githubusercontent.com/NVIDIA/OpenShell/main/install.sh | sh
   # or: uv tool install -U openshell
   openshell status        # must report the gateway reachable
   ```
2. Copy and fill in the policy template — substitute every `__PLACEHOLDER__`
   (`__AGENT_USER__`, `__MAC_HUB_HOST__`/`__MAC_HUB_PORT__`, `__MODEL_GATEWAY_HOST__`)
   for your fleet, and make the Hermes runtime reachable inside the sandbox via a
   prebuilt image (`--from`) or upload (see `MAC_OPENSHELL_CREATE_ARGS`):
   ```bash
   cp deploy/openshell/mac-hermes-policy.yaml /etc/mac/openshell-policy.yaml
   $EDITOR /etc/mac/openshell-policy.yaml
   ```
3. Set the executor environment (on the worker / in the fleet agent config):
   ```bash
   export MAC_OPENSHELL_SANDBOX=1
   export MAC_OPENSHELL_POLICY=/etc/mac/openshell-policy.yaml
   # make the runtime + workspace available inside the sandbox, e.g.:
   export MAC_OPENSHELL_CREATE_ARGS="--from my-mac-hermes-image"
   ```

### Environment knobs

| Variable | Default | Meaning |
| --- | --- | --- |
| `MAC_OPENSHELL_SANDBOX` | _(off)_ | truthy → wrap the agent in OpenShell |
| `MAC_OPENSHELL_BIN` | `openshell` | path to the `openshell` binary |
| `MAC_OPENSHELL_POLICY` | _(none)_ | path to the guardrail policy YAML |
| `MAC_OPENSHELL_SANDBOX_NAME` | _(ephemeral)_ | fixed sandbox name (debug only) |
| `MAC_OPENSHELL_KEEP` | _(off)_ | truthy → `--keep` (don't tear down; debug) |
| `MAC_OPENSHELL_CREATE_ARGS` | _(none)_ | extra `sandbox create` args (shell-split), e.g. `--from img`, `--upload /src:/src` |
| `MAC_OPENSHELL_ENV_PASSTHROUGH` | hub+gateway vars | comma list of env names forwarded via `--env` |
| `MAC_RELAY_OBSERVABILITY` | _(off)_ | truthy → emit NeMo Relay scopes/events (needs `nemo_relay`) |

## Verification (requires Docker + OpenShell installed)

This session could not run OpenShell end-to-end (no Docker daemon, CLI not
installed, Python 3.14 venv). To verify on a host that has them:

1. `openshell status` reports the gateway up.
2. Dry-run the wrap without spawning:
   ```python
   import os; os.environ["MAC_OPENSHELL_SANDBOX"]="1"
   os.environ["MAC_OPENSHELL_POLICY"]="/etc/mac/openshell-policy.yaml"
   from mac import task_executor as te
   print(te._maybe_wrap_openshell(te._hermes_argv("hello")))
   ```
   Confirm it begins with `openshell sandbox create … --policy … --` and ends
   with the Hermes argv.
3. Run one real task with `MAC_OPENSHELL_SANDBOX=1`. Confirm: the agent
   completes; a network attempt **not** in the policy is **denied**
   (`openshell logs <sandbox> --since 5m` shows `action=deny`); evidence is
   written to the workspace.
4. Enable OCSF JSONL and confirm sandbox events flow into mac observations:
   ```bash
   openshell settings set --global --key ocsf_json_enabled --value true
   ```
   then feed `/var/log/openshell-ocsf.*.log` through
   `relay_observability.parse_ocsf_lines(...)`.

## Deferred work (tracked as `mac task` tickets)

- **Provision OpenShell on fleet hosts** + build a sandbox image containing the
  Hermes runtime (`~/.mac/venv` + `~/.mac/src`) so `--from` works.
- **Tune the policy** against the real model-gateway and hub hosts; decide
  `landlock.compatibility: hard_requirement` for production.
- **Wire the OCSF → observation bridge** into a running ingestion path
  (`ObservabilityService.record_log`) and `mac observability` views.
- **NeMo Relay phased rollout**: scopes at the request/task boundary → managed
  tool/LLM calls → guardrail/sanitizer middleware → exporters (keep the SQLite
  sink; add OTLP/ATIF/OpenInference). The Hermes loop-detection guardrail and
  input sanitizers are *correctness*, not permissions — port them to Relay
  middleware; do **not** port the dangerous-command approval layer (it is
  disabled under YOLO because OpenShell owns permissions).
- **End-to-end verification** on a Docker-enabled host (the steps above).
