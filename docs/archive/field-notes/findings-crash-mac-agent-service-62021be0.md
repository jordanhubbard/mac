!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Findings: mac-agent-service startup self-test crash (62021be0)

- Scope: investigation only. No source is modified by this record.
- Parent: "P0: repair MAC crash mac-agent-service at 62021be0356c" (repository contract for project `mac`).
- Crash signature: process `mac-agent-service`, exit code `1`, stack line
  `agent startup self-test: failed to report heartbeat: TimeoutError: timed out`.
- Baseline reviewed: the tree at this worktree's base revision.

## Ground truth

The crash and its fix are already present and effective on the current base. This
investigation reproduced the regression via the checked-in test and confirmed the
remediation is in place, so the follow-up implementation work is a
**contract-gate / evidence-mechanics** concern, not a fresh code fix.

- Root cause (historical): a *transient* shared-service probe timeout
  (Qdrant / Firecrawl) or hub heartbeat timeout in the embedded
  `mac-agent-startup-self-test` Python heredoc inside `deploy/fleet-node-install.sh`
  was recorded as a *blocking* problem, forcing `sys.exit(1)`. The
  `mac-agent-service` wrapper ran the self-test under `set -euo pipefail` with no
  exit-code guard, so that non-zero exit crashed the service before the worker
  started. The `failed to report heartbeat` line is incidental, non-blocking noise.
- Fix already on base (verified): the self-test now classifies transient
  read/connect timeouts as `transient_problems` that are demoted to non-blocking
  (`status: degraded`, `sys.exit(0)`), while a genuine misconfiguration (refused
  connection / bad endpoint) stays blocking (`status: failed`, `sys.exit(1)`). The
  wrapper now captures the self-test exit code and only exits `1` on a real
  blocking verdict, degrading (and continuing) on any other non-zero exit.

## Root-cause location (by file / function)

- `deploy/fleet-node-install.sh` — `mac-agent-startup-self-test` Python heredoc:
  `is_transient_timeout()`, the bounded-retry `probe_http()` loop, the
  `transient_problems` accumulation for the mandatory Qdrant / Firecrawl / hub
  probes, and the `blocking_problems`/`sys.exit` verdict.
- `deploy/fleet-node-install.sh` — `install_mac_agent_wrapper()` `mac-agent-service`
  body: the guarded `selftest_rc` capture around
  `mac-agent-startup-self-test` (`exit 1` only on a blocking verdict).

## Tests that cover the affected code

- `tests/test_selftest_transient_timeout_crash.py` (present on base) is the
  regression: it extracts and execs the self-test heredoc with the shared-service
  probes stubbed and asserts (a) a transient `TimeoutError` degrades non-blocking
  and exits `0`, (b) a `TimeoutError` wrapped in `urllib.error.URLError` behaves the
  same, and (c) a `ConnectionRefusedError` still blocks and exits `1`. All three
  pass against the base.

## Contract-gate commands

- Toolchain / bootstrap: `python3 scripts/bootstrap-project.py` (creates
  `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/coverage`).
- Canonical verification: `scripts/run-contract-tests.sh`.
- Focused required tests (`.mac/project.yaml` `focused_required_tests`):
  `tests/test_openshell_certifier.py`, `tests/test_publication_lane.py`,
  `tests/test_repository_contract_certification.py`.

## Guidance for the implementation child

- Do **not** re-harden the Python transports (`src/mac/api_client.py`,
  `src/mac/http_client.py`) or treat the `failed to report heartbeat` log line as
  the fault — those layers are already safe and are not on the crash's causal path.
- The self-test fix and the wrapper guard are already on base; the regression test
  passes. Focus remaining effort on satisfying the repository contract gate
  (bootstrap + `scripts/run-contract-tests.sh` + the focused required tests) and on
  clean commit/push mechanics, since that is where the parent failed.
