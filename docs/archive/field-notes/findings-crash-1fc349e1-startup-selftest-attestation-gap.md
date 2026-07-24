!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Findings: startup self-test attestation-gap crash (crash_1fc349e109ed4ff9885acf1c8ba99948)

- Task: `task_87337b9d0c1f47dca6dd79c05a0b8115` (investigation only; no production behavior change here).
- Parent: `task_d965b4225fc247318c97f9d2678f4579` — "P0: repair MAC crash mac-agent-service at b7a6558a".
- Prior CANCELLED repair: `task_c5f3f6e04bd847179d14b772a05aa941`.
- Crash fingerprint: `sha256:f0e0e4f1...`
- Process: `mac-agent-service`, exit code `1`.
- Affected node: an OpenShell **loop worker** (`MAC_OPENSHELL_SANDBOX` truthy, `MAC_WORKER_MODE=loop`).
- Baseline reviewed: repo revision `b7a6558a` (verified against the equivalent tree in this worktree).

## Summary (root cause)

The crash is a **transient / registration-pending attestation gap misclassified
as a permanent, blocking misconfiguration**. It originates in the embedded
`mac-agent-startup-self-test` Python heredoc inside `deploy/fleet-node-install.sh`
(function `install_mac_agent_wrapper`), not in the Python worker.

On an OpenShell loop worker, the self-test probes the report-repository executor
attestation:

```python
report_executor_attestation: dict[str, object] = {}
if openshell_enabled and str(os.environ.get("MAC_WORKER_MODE") or "").strip() == "loop":
    try:
        from mac.worker import _read_only_report_executor_attestation
        observed_report_attestation = _read_only_report_executor_attestation(
            [str(mac_home / "bin" / "mac-task-executor")]
        )
    except Exception as exc:
        observed_report_attestation = None
        problems.append("report repository executor attestation probe failed: " + safe_error(exc))
    if not isinstance(observed_report_attestation, dict):
        problems.append(
            "report repository executor lacks the exact hardened OpenShell attestation"
        )
    else:
        report_executor_attestation = observed_report_attestation
        checks["report_repository_executor_attestation"] = True
else:
    checks["report_repository_executor_attestation"] = not openshell_enabled
```

`mac.worker._read_only_report_executor_attestation(...)` is intentionally
**fail-closed** and returns `None` (a non-dict) in a wide range of boot-time,
still-recoverable situations — for example when the executor binary/script,
OpenShell policy, runtime image, or Landlock posture cannot yet be attested at
startup, or before the controller has approved the executor. When it returns a
non-dict, the self-test appends the string

> `report repository executor lacks the exact hardened OpenShell attestation`

to `problems`. Crucially, that problem is **never added to
`non_blocking_problems`** (only OpenClaw gateway problems and
`transient_problems` are demoted). It therefore falls straight through to
`blocking_problems`:

```python
if openclaw_serves_gateway:
    non_blocking_problems = list(openclaw_agent_probe_problems)
else:
    non_blocking_problems = list(openclaw_problems)
for problem in transient_problems:
    if problem not in non_blocking_problems:
        non_blocking_problems.append(problem)
blocking_problems = [problem for problem in problems if problem not in non_blocking_problems]
status = "passed"
if blocking_problems:
    status = "failed"
elif problems:
    status = "degraded"
...
sys.exit(1 if blocking_problems else 0)
```

`blocking_problems` non-empty → `status="failed"` → `sys.exit(1)`. The
`mac-agent-service` wrapper then converts that exit-1 verdict into `exit 1`,
stopping the service (see the exit-code contract below).

## Ground-truth reproduction (confirmed in this worktree)

Executing the extracted self-test heredoc against a temporary HOME as an
OpenShell loop worker — with **all** mandatory shared services satisfied
(Qdrant/Firecrawl configured, probes stubbed OK) so the attestation gap is the
*only* problem — yields:

```text
EXIT_CODE= 1
STATUS= failed
PROBLEMS= ['report repository executor lacks the exact hardened OpenShell attestation']
BLOCKING= ['report repository executor lacks the exact hardened OpenShell attestation']
NON_BLOCKING= []
TRANSIENT= []
```

This is the crash: a single boot-time attestation gap, with nothing else wrong,
drives `sys.exit(1)` and stops `mac-agent-service`.

## The gap is transient and self-heals — CONFIRMED

The attestation is re-probed on every heartbeat and re-attached once it becomes
obtainable, in `src/mac/worker.py`:

```python
def _resources_with_live_report_executor_attestation(self, resources):
    """Replace the report claim with a fresh local artifact probe."""
    refreshed = dict(resources or {})
    refreshed.pop(REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY, None)
    refreshed.pop(REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY, None)
    executor_argv = list(self.executor.argv) if isinstance(self.executor, SubprocessExecutor) else []
    attestation = _read_only_report_executor_attestation(executor_argv)
    if attestation is not None:
        refreshed[REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY] = attestation
    return refreshed
```

`_heartbeat(...)` calls this on each beat, so a boot-time gap heals on the next
heartbeat without any restart. A verdict that permanently stops the service for a
condition that recovers within one heartbeat is therefore incorrect: this
belongs in the **degraded** (non-blocking, exit 0, keep running) bucket, exactly
like the shared-service transient-timeout and gateway-less worker cases.

## Exit-code contract (wrapper) — CONFIRMED

`install_mac_agent_wrapper()` writes `~/.mac/bin/mac-agent-service` which already
captures the self-test exit code (a previously-applied hardening):

```console
selftest_rc=0
"$HOME/.mac/bin/mac-agent-startup-self-test" || selftest_rc=$?
if [ "$selftest_rc" -eq 1 ]; then
  exit 1
elif [ "$selftest_rc" -ne 0 ]; then
  echo "mac-agent-service: startup self-test exited $selftest_rc; continuing degraded" >&2
fi
```

Contract: **exit 1 = blocking misconfiguration → stop the service**; any other
non-zero = internal self-test fault → degrade but keep running; exit 0 =
passed/degraded → keep running. So the fix must keep the attestation-gap verdict
OUT of `exit 1`, i.e. out of `blocking_problems` for the transient case.

## Comparison against established degraded patterns — CONFIRMED

Two existing regressions encode the intended "transient/decoupled → degrade,
permanent misconfig → block" shape; the attestation gap should follow the same
model:

- `tests/test_selftest_transient_timeout_crash.py`: a mandatory Qdrant/Firecrawl
  (or hub) probe that only ever *times out* after bounded retries is recorded in
  `transient_problems`, demoted to `non_blocking_problems`, `status="degraded"`,
  exit 0 — while a deterministic `ConnectionRefusedError` stays blocking (exit 1).
- `tests/test_gatewayless_worker_selftest_crash.py`: a worker whose OpenClaw
  gateway artifacts are simply not installed has its OpenClaw problems demoted to
  non-blocking (worker/gateway decoupling), exit 0 — while a gateway-serving node
  with a broken gateway still fails hard.

Both keep a genuine, deterministic misconfiguration blocking. The attestation
gap needs the same split.

## Intended semantics (fix design for the implementation child)

Distinguish a **transient / registration-pending attestation gap** (DEGRADE)
from a **genuine permanent misconfiguration** (BLOCK):

- DEGRADE (non-blocking, exit 0, keep the service running, re-probe on heartbeat)
  when the executor is simply not attestable *yet* — the executor/script,
  OpenShell policy, runtime image, controller approval, or Landlock posture is
  not available at boot but is expected to heal via
  `mac.worker._resources_with_live_report_executor_attestation` on the next
  heartbeat. The non-dict `None` return with no explicit misconfiguration signal
  is this case.
- BLOCK (remain a blocking problem, exit 1) for a genuine, deterministic
  misconfiguration that will NOT self-heal, e.g.:
  - invalid `MAC_OPENSHELL_CREATE_ARGS` (parse error or forbidden `--env` / `--`);
  - missing / non-executable `MAC_OPENSHELL_BIN` (openshell binary absent);
  - wrong executor backend (`MAC_EXECUTOR_BACKEND != hermes`);
  - unsafe passthrough (`PATH` in `MAC_OPENSHELL_ENV_PASSTHROUGH`),
    `MAC_OPENSHELL_KEEP` set, or the legacy/non-`mac-task-executor` alias.

Note that several of those permanent conditions are **already** surfaced as
separate blocking problems earlier in the self-test (the
`MAC_OPENSHELL_CREATE_ARGS` / `OpenShell sandbox is enabled but MAC_OPENSHELL_BIN
is not executable` checks feeding `checks["openshell_executor_config"]`). That
means the attestation-gap string itself is, in practice, the *residual*
transient signal once those explicit misconfig checks have passed — which is
precisely why it should DEGRADE rather than BLOCK.

### Minimal fix surface (smallest-first; NOT applied here)

1. Record the attestation-gap problem in a non-blocking bucket for the loop
   worker: append the "lacks the exact hardened OpenShell attestation" message
   (and the "attestation probe failed" message) to `transient_problems` (or an
   equivalent attestation-specific non-blocking list) so it is demoted to
   `non_blocking_problems`, yielding `status="degraded"` and exit 0 — while the
   explicit `MAC_OPENSHELL_CREATE_ARGS` / `MAC_OPENSHELL_BIN` / backend /
   passthrough misconfiguration checks remain in `problems` and stay blocking.
2. Add a regression paralleling the two above
   (`tests/test_selftest_attestation_gap_crash.py`): an OpenShell loop worker
   with a non-dict attestation but otherwise-valid config → exit 0, degraded,
   attestation message in `non_blocking_problems`; a genuine permanent
   misconfiguration (bad `MAC_OPENSHELL_CREATE_ARGS` or missing binary) → exit 1,
   failed, blocking.

## Prior CANCELLED repair delta to avoid repeating

The prior repair task's hub-side evidence is not reachable from this sandbox: the
hub API here rejects unauthenticated requests (`HTTP 403 "missing bearer token"`)
and no `MAC_TOKEN`/`MAC_WORKER_TOKEN` is present in this environment, so the
CANCELLED repair's exact diff could not be read from the hub. What the current
*code state* demonstrates:

- The `mac-agent-service` wrapper already captures the self-test exit code and
  only stops the service on `exit 1` (the "continuing degraded" branch exists).
  So a repair that only touches the wrapper guard is insufficient — the
  attestation gap still returns exactly `1` from the self-test, which the wrapper
  correctly treats as blocking.
- The transient-timeout and gateway-less degraded machinery
  (`transient_problems`, `openclaw_problems`, the `non_blocking_problems`
  demotion) already exists, but the attestation-gap message was **never wired
  into any non-blocking list**.

Prior-repair delta to avoid: **do not** re-touch the wrapper exit-code guard or
re-implement the transient/gateway degraded scaffolding, and **do not** try to
make `_read_only_report_executor_attestation` return a dict at boot (it is
deliberately fail-closed). The fix is the single missing wiring: demote the
transient attestation-gap problem to non-blocking in the self-test heredoc while
keeping the explicit permanent-misconfig checks blocking.

## Verification baseline (this task)

- `tests/test_gatewayless_worker_selftest_crash.py` and
  `tests/test_selftest_transient_timeout_crash.py`: **4 passed** (current
  baseline; unchanged by this investigation).
- Ground-truth reproduction above confirms the attestation gap alone yields
  `exit 1` / `status=failed` today.

No production behavior is changed by this investigation task; this note is the
sole deliverable.
