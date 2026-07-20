!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Findings: startup self-test transient-timeout crash (crash_591645a352fc4d54bf5e3f99384da7dc)

- Task: `task_7ede42de42a34710b9c73ab38266feef` (investigation only; no production fix here).
- Parent: `task_a99d9a07501348258709b8e32f19077e` — "P0: repair MAC crash mac-agent-service at 62021be0356c".
- Crash fingerprint: `sha256:bc6d7cb9...`
- Process: `mac-agent-service`, exit code `1`.
- Stack signature: `agent startup self-test: failed to report heartbeat: TimeoutError: timed out`.
- Baseline reviewed: repo revision `62021be0` (verified against the equivalent tree in this worktree).

## Summary (root cause)

The crash is a **transient timeout treated as a permanent, blocking failure**. It
originates in the embedded `mac-agent-startup-self-test` Python heredoc inside
`deploy/fleet-node-install.sh`, not in the Python worker or its HTTP clients.

A single 10s HTTP probe of a *mandatory* dependency (Qdrant or Firecrawl) that
times out is recorded as a blocking `problems` entry, which forces
`sys.exit(1)`. Because the `mac-agent-service` wrapper runs the self-test under
`set -euo pipefail` with **no `|| true` guard**, that non-zero exit crashes the
service with exit code `1` before the worker ever starts.

The `failed to report heartbeat: TimeoutError` line in the stack signature is
**incidental, non-blocking noise**: the heartbeat POST is wrapped in
`try/except (OSError, urllib.error.URLError)` and only logged. It never changes
the exit code. The real killer is the mandatory-dependency probe failure, which
is separately appended to `problems` and lands in `blocking_problems`.

## Evidence in the repo (baseline 62021be0)

### (a) Single-shot 10s probe, no retry, blocking on TimeoutError — CONFIRMED

`deploy/fleet-node-install.sh` `probe_http()`:

```python
def probe_http(path_base, suffix, headers=None):
    ...
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            response.read(1_048_576)
        return True, ""
    except (OSError, urllib.error.URLError) as exc:
        return False, safe_error(exc)
```

- Exactly one `urlopen(..., timeout=10)`; there is **no retry loop, no backoff,
  no `time.sleep`** anywhere in the probe path (grep for `retry|range(|sleep`
  finds none in the self-test block).
- `TimeoutError` is an `OSError` subclass, so a transient read/connect timeout is
  swallowed into `return False, "TimeoutError: timed out"`.

The mandatory-dependency call sites append a **blocking** `problems` entry on any
non-OK probe:

```python
elif qdrant_url:
    ok, error = probe_http(qdrant_url, "/collections", qdrant_headers)
    if not ok:
        problems.append(f"Qdrant shared memory endpoint is unreachable: {error}")
    checks["qdrant_shared_memory"] = ok
...
elif firecrawl_url:
    ok, error = probe_http(firecrawl_url, "/health", firecrawl_headers)
    if not ok:
        problems.append(f"Firecrawl web search endpoint is unreachable: {error}")
    checks["firecrawl_web_search"] = ok
```

Only OpenClaw problems can be demoted to non-blocking:

```python
if openclaw_serves_gateway:
    non_blocking_problems = []
else:
    non_blocking_problems = list(openclaw_problems)
blocking_problems = [p for p in problems if p not in non_blocking_problems]
...
sys.exit(1 if blocking_problems else 0)
```

So a Qdrant/Firecrawl timeout is **never** demoted and always drives
`sys.exit(1)`.

### (b) Wrapper runs self-test under `set -euo pipefail` with no guard — CONFIRMED

`install_mac_agent_wrapper()` writes `~/.mac/bin/mac-agent-service`:

```console
#!/usr/bin/env bash
set -euo pipefail
...
if [ "${MAC_AGENT_STARTUP_SELF_TEST:-1}" != "0" ]; then
  "$HOME/.mac/bin/mac-agent-startup-self-test"
fi
```

The self-test is invoked bare — no `|| true`, no `if`-captured exit code — so its
`exit 1` propagates as the wrapper's exit under `errexit`, crashing
`mac-agent-service` with code `1`.

### (c) Heartbeat-report block is non-blocking and incidental — CONFIRMED

```python
try:
    urllib.request.urlopen(req, timeout=10).read()
except (OSError, urllib.error.URLError) as exc:
    # HTTPError (e.g. HTTP 400) is a URLError subclass, so a rejected heartbeat
    # is logged but never propagates or changes the self-test exit code: only
    # blocking_problems decide whether the worker starts.
    print(f"agent startup self-test: failed to report heartbeat: {safe_error(exc)}", ...)
```

The heartbeat timeout produces the log line seen in the stack signature but does
**not** append to `problems` and does **not** affect the exit code.

### worker.py `--heartbeat-only` and client TimeoutError wrapping — CONFIRMED SAFE

- `src/mac/worker.py` `main()` `--heartbeat-only` path calls `client.post(...)`
  and the outer `try` only catches `MacApiError`.
- `src/mac/api_client.py` `MacApiClient.request()` explicitly wraps bare
  `OSError` (including `TimeoutError`) into `MacApiError`, so a heartbeat timeout
  on this path is caught and returns exit `1` cleanly via the guarded handler —
  it does not escape as an unhandled `TimeoutError`.
- `src/mac/http_client.py` `HubClient._urllib_transport()` similarly wraps bare
  `OSError`/`TimeoutError` into `HubClientError`.

Conclusion: the Python worker/client transport is already hardened against bare
timeouts. **The remaining gap is exclusively the shell self-test heredoc**, which
has its own independent `urllib` probe and its own exit logic that does not share
the Python retry/wrap hardening.

## mac-g55y legacy-env warning — unrelated

`src/mac/fleet_env.py` emits a one-shot `logging.warning("using legacy flat env
var ...; see mac-g55y ...")` when a flat `MAC_*` credential is read instead of a
fleet-scoped variant. It is a deprecation *warning* only, log-level `WARNING`,
with no effect on exit codes or blocking problems. It co-occurs in logs because
fleet nodes still carry flat env vars, but it is not part of the crash causal
chain.

## Prior failed repair (task_bdf3ac01bd9c480f8f0097f29a73f96d)

The prior repair task's hub-side evidence is not reachable from this sandbox
(no `mac`/hub access; the referenced repair task id and crash ids do not appear
in the repo tree). What the *code state* demonstrates about the prior attempt:

- The Python transport clients (`api_client.py`, `http_client.py`) already carry
  the `OSError`-wrapping fix, complete with comments referencing a
  `TimeoutError`-killing-`mac-agent-service` incident. This strongly indicates a
  prior repair **hardened the Python side** (client transport + heartbeat path).
- That fix was **ineffective for this crash** because the crash does not flow
  through those Python clients at all. The startup self-test is a standalone
  heredoc using its own `urllib` calls with independent, still-unhardened exit
  logic. Hardening `MacApiClient`/`HubClient` cannot change the self-test's
  `sys.exit(1)`.

Prior-repair delta to avoid repeating: **do not** re-harden or re-wrap the
Python `api_client.py`/`http_client.py` transports again, and do not treat the
`failed to report heartbeat` log line as the fault — both are already handled
and/or non-causal. Fixing the wrong layer here will pass Python unit tests while
leaving the shell self-test crash fully intact.

## Minimal fix surface (for the follow-up repair task; NOT applied here)

The correct, minimal remediation is confined to the `mac-agent-startup-self-test`
heredoc in `deploy/fleet-node-install.sh`. Options, smallest-first:

1. **Make transient probe timeouts non-fatal / retried.** Give `probe_http` a
   short bounded retry (e.g. 2-3 attempts with a small backoff) before declaring
   a mandatory dependency unreachable, so a single hub/dependency blip does not
   crash startup.
2. **Do not classify a *transient timeout* as a blocking problem.** Distinguish
   `TimeoutError`/`ConnectionError` (transient) from a definitive negative
   (connection refused / 4xx / 5xx) and treat the transient case as
   `degraded`/non-blocking (report `idle`+`degraded`, let the worker start and
   re-probe), rather than `offline`+blocking → `sys.exit(1)`.
3. Optionally add a `|| true`-style guard or explicit exit-code capture in the
   `mac-agent-service` wrapper so a self-test failure degrades rather than hard-
   crashes the service — but option 1/2 is preferable because a truly missing
   mandatory dependency should still be surfaced as blocking.

No production behavior is changed by this investigation task; this note is the
sole deliverable.
