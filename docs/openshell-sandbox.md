# Running Hermes under the OpenShell sandbox

This describes the optional OpenShell sandbox seam for the task executor: launch
the Hermes agent as a confined child of an [OpenShell](https://github.com/NVIDIA/OpenShell)
sandbox so OpenShell is the **sole guardrail authority**, letting the agent run
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

`_maybe_wrap_openshell()` in `src/mac/task_executor.py` — a pure argv transform
applied at the launch seam. When `MAC_OPENSHELL_SANDBOX` is truthy it wraps the
Hermes invocation in `openshell sandbox create … -- <argv>`; otherwise it
returns the argv unchanged (proven by `tests/test_openshell_sandbox.py`), so the
executor behaves exactly as before when disabled. The default guardrail policy
template lives at `deploy/openshell/mac-hermes-policy.yaml`.

> Observability is handled separately by the NeMo Relay seam
> (`src/mac/relay_observability.py`, Phase 1 merged; Phase 2 in PR #133).
> OpenShell's OCSF event stream (allowed/denied network, process, findings) can
> be fed into that pipeline as a follow-up.

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
3. Run one real task with `MAC_OPENSHELL_SANDBOX=1`: the agent completes,
   evidence is written, the sandbox tears down (one-shot), and an off-policy
   network attempt is **denied** (`openshell logs <sandbox> --since 5m` shows
   `action=deny`).

## Follow-up

- Build a sandbox image (or `--upload` payload) containing the Hermes runtime
  (`~/.mac/venv` + `~/.mac/src`) so `--from` works on fleet hosts.
- Tune the policy against the real model-gateway/hub hosts; consider
  `landlock.compatibility: hard_requirement` for production.
- Feed OpenShell's OCSF event stream into the NeMo Relay / observability
  pipeline.
