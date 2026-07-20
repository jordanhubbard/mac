!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Investigation: mac environment-prerequisite finding is a classifier false positive

**Task**: Establish the real, reproducible cause behind the environment-prerequisite
repair chain minted for the mac "dream-repair prerequisite finding" parent (the
grandparent that exhausted its attempts with `failure_class == "environment"`) and
its environment-prerequisite repair child. Determine whether a prerequisite is
genuinely broken (and where) or whether the finding is a false positive that should
be closed. Investigation only — no product-code edits beyond this note/evidence.

**Investigated by**: fleet worker (read-only inspection plus targeted reproduction
in a task-owned worktree; parent history/salvage read via the hub API).

## Status: FALSE POSITIVE — no environment prerequisite is broken

The "environment" failure class was produced by a misclassification in
`src/mac/attempt_failure_classifier.py`, not by a missing toolchain command,
bootstrap step, venv artifact, credential source, or contract-test gate. The
underlying grandparent failure was an executor wall-clock **timeout** (process exit
code 124) with no captured output — a scope/"task too large" failure, not an
environment failure. Decision: **REPAIR the classifier** (a real, reproducible
bug) so the finding stops recurring; there is no environment prerequisite for the
repair child to provision.

## Ground truth from the parent/grandparent evidence

Read from the hub API (`GET /tasks/{id}`, task memory records) for the grandparent
task and its environment-prerequisite repair child:

- The grandparent exhausted all 3 attempts. Every attempt transitioned
  `running -> blocked` with an identical diagnosis detail:
  `failure = "executor_failed"`, `reason = "executor_failed"`, `returncode = 124`,
  `manual_repair_required = true`, and
  `output_tail_unavailable_reason = "transition supplied no stdout, stderr, output,
  log, or tail field"` (i.e. **no output was captured at all**).
- The three salvage `deployment_learning:mac` memory records are identical in
  shape: `outcome = "failure"`, `error_signature = ""` (empty),
  `signals = {checks_pass: null, files_changed: null, pushed: null, tests: null,
  returncode: 124}`. No test ran, nothing was pushed, no files changed — only the
  numeric return code 124 is present.
- Exit code **124** is the canonical `timeout(1)` / wall-clock-timeout code. The
  repo's own diagnosis mapping (`_failure_diagnosis` in `src/mac/services.py`) reads
  rc=124 as "Agent run timed out — the task is likely too large for one run;" raise
  `MAC_EXECUTOR_AGENT_TIMEOUT` and/or split into child tasks.

Conclusion from the evidence: the grandparent did not fail on a prerequisite. It
ran out of wall-clock time before it could emit output, tests, or a push.

## Reproduction in-sandbox (task-owned worktree, bootstrapped .venv)

Measured with `python3` 3.12.13, `git` 2.39.5 (>= 2.38), `gh` 2.95.0.

- Toolchain: all required commands (`python3`, `git`, `gh`) present; `git` meets
  the >= 2.38 merge-tree requirement. `missing_after == []`.
- Bootstrap: `python3 scripts/bootstrap-project.py` succeeds in ~2s and creates all
  contract artifacts `.venv/bin/{python,pytest,coverage}`.
- Contract harness: `scripts/run-contract-tests.sh` resolves an interpreter
  (`.venv/bin/python`), and the suite-capability probe passes
  (`coverage`, `pytest`, and `import cryptography, fastapi, yaml` all succeed).
- Infra tests pass: `tests/test_environment_contract.py`,
  `tests/test_worker_environment_contract_wiring.py`,
  `tests/test_openshell_bootstrap_contract.py`, `tests/test_k8s_bootstrap.py`,
  `tests/test_k8s_bootstrap_edges.py` — 62 + 67 = 129 tests, all green.
- Suite size: `pytest --collect-only` reports **6686 tests**, run under
  `coverage run` with `patch = ["subprocess"]` (parallel coverage for every Python
  child). This is a very large suite and is a plausible source of an executor
  wall-clock timeout (rc=124) on a slower/loaded host.

So on this sandbox the prerequisites are all satisfied; the environment finding does
not reproduce as an environment defect. It reproduces only as a **classification** of
the grandparent's timeout history.

## Root cause: numeric `returncode: 124` never matches the scope markers

`classify_attempt_failure` -> `_class_from_history`
(`src/mac/attempt_failure_classifier.py`) buckets an attempt as `scope` when the
event blob contains a timeout marker, and as `environment` when it contains an
infra marker. `saw_scope` is checked before `saw_environment`, so a timeout should
win. But:

- The scope timeout markers are string forms: `"timed out"`, `"timeout"`,
  `"rc=124"`, `"returncode 124"` (space), `"code: 124"`.
- The grandparent detail carries the timeout ONLY as a numeric field
  `"returncode": 124`. `_compact_text` JSON-dumps the detail, rendering it as
  `"returncode": 124` (a **colon**, not a space and not `rc=`). None of the scope
  markers match that rendering.
- Meanwhile the same detail's `reason`/`failure` is the string `"executor_failed"`,
  which IS in the environment marker list.

Net effect: `saw_scope` stays `False`, `saw_environment` becomes `True`, and the
history is classified `environment`. That mints the environment-prerequisite repair
chain for what is actually a scope/timeout failure.

Reproduced directly against the grandparent's real diagnosis detail:
`_class_from_history([...]) == "environment"`, with
`"returncode 124" in _compact_text(detail) == False`,
`"rc=124" in _compact_text(detail) == False`, and
`"executor_failed" in _compact_text(detail) == True`.

Note also that `executor_failed` is an ambiguous, generic executor-abort signal
(it is the diagnosis for a plain wall-clock timeout here) yet it is treated as an
unconditional environment marker. The combination of (a) numeric-rc timeouts not
matching the scope markers and (b) `executor_failed` being an environment marker is
what turns a timeout into a false "environment prerequisite".

## Decision: REPAIR (classifier), not provision an environment prerequisite

There is nothing to provision — the agent/service/credential-source/toolchain are
all present and the contract bootstrap/tests pass. The durable fix is to stop the
classifier from mislabeling numeric-rc timeouts as `environment`.

### Minimal, targeted fix for the repair child

Edit only `src/mac/attempt_failure_classifier.py`:

1. Detect a numeric timeout return code as a scope signal. In `_class_from_history`
   (or a small helper it calls), inspect the event `detail` for a numeric
   `returncode`/`return_code`/`exit_code`/`rc`/`code` equal to `124` and set
   `saw_scope = True`. This is the standard `timeout(1)` code and already maps to
   "task too large" everywhere else in the codebase.
2. Do NOT let a bare `executor_failed` signal alone force `environment` when a
   timeout signal (string OR numeric rc=124) is present in the same history. Because
   `saw_scope` is already checked before `saw_environment`, step 1 alone is
   sufficient to flip this case to `scope`; optionally tighten `executor_failed` so
   it does not count as `environment` when a timeout indicator co-occurs.

Keep the change surgical — no behavior change for genuine environment markers
(clone/auth/network/`command not found`/`no such file or directory`, etc.).

### Contract tests that must pass

- `tests/test_attempt_failure_classifier.py` — extend it with a regression case
  that feeds the real grandparent shape (`detail = {reason: "executor_failed",
  failure: "executor_failed", returncode: 124, ...}`, no string timeout marker) and
  asserts `classify_attempt_failure(...).failure_class == "scope"`. The current file
  only covers the string form `error: "returncode 124"` (around
  `tests/test_attempt_failure_classifier.py:52`), which is exactly why this
  numeric-rc case slipped through.
- `scripts/run-contract-tests.sh` — the full contract suite must stay green.

### Files the repair child should touch

- `src/mac/attempt_failure_classifier.py` (the fix).
- `tests/test_attempt_failure_classifier.py` (the regression test).

No other product code, scripts, or infra files require changes for this finding.

## Consequential assumptions

- The grandparent's numeric `returncode: 124` is a genuine executor wall-clock
  timeout (standard `timeout(1)` exit code), corroborated by the empty
  `output_tail`, empty `error_signature`, and all-null test/push/files signals.
- The 6686-test contract suite under `coverage run` is the plausible trigger for
  that timeout on a slower/loaded host; the sandbox here is fast enough that the
  prerequisites all pass, which is why the finding presents as a false positive.
- This repair child's origin type is `direct_task` (not `_prerequisite`) in the
  task payload delivered to this worker, so the dead-letter-of-repair caveat in the
  task description does not gate this investigation note.
