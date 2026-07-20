!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Assessment: task_1b67831356c347c3a91d782982f47d1c

**Task**: Confirm the low-confidence dream-cycle repair finding
`dreamrepair:403efe8e3a9f5d1488c67772169490d1` about `hermes -z` one-shot
clean-slate isolation before changing production behavior.
**Origin candidate**: prior repair task_9441fc0eb0644218a8e265cefed97d7e
(the repair attempt whose reported failure seeded this finding).
**Finding kind**: dream-cycle repair finding — scope project — confidence low.
**Repo areas**: `src/mac/_hermes/hermes_cli/oneshot.py`,
`tests/test_hermes_oneshot_session_isolation.py`.
**Assessment date**: 2026-07-16
**Assessed by**: fleet worker (investigation; no production `oneshot.py` edits).

## Status: CLOSED — the isolation regression is ALREADY FIXED in production; the characterization test file is stale/self-contradictory and is the real remaining actionable gap

The `hermes -z` one-shot clean-slate isolation that the prior repair attempted
is present in the shipped code. The reported "repair did not land" concerned the
*mechanism* (an `isolated` parameter), not the outcome: isolation was instead
achieved by passing `skip_memory=True` / `skip_context_files=True`
unconditionally at the `AIAgent(...)` call site. No production change is needed.
The follow-up work is to rewrite the stale characterization tests so they match
the shipped isolation contract.

## Ground Truth Observed

Measured in the task-owned worktree with the bootstrapped `.venv`
(`python3` 3.12.13; `git`/`gh` present; pytest/coverage installed by
`python3 scripts/bootstrap-project.py`). Production `oneshot.py` was read, not
modified.

### 1. Clean-slate isolation is present at the AIAgent call site

`src/mac/_hermes/hermes_cli/oneshot.py::_run_agent()` builds the agent with
unconditional isolation kwargs:

- `src/mac/_hermes/hermes_cli/oneshot.py:335` — `agent = AIAgent(`
- `src/mac/_hermes/hermes_cli/oneshot.py:350` — `skip_memory=True,`
- `src/mac/_hermes/hermes_cli/oneshot.py:351` — `skip_context_files=True,`

So each `hermes -z` invocation skips cross-turn memory recall/write and
repo context-file loading — the clean-slate behavior the finding asked about.

### 2. No `isolated` parameter exists on the current signatures

Neither entry point exposes the `isolated` parameter the prior repair claimed to
add:

- `src/mac/_hermes/hermes_cli/oneshot.py:125` — `def run_oneshot(prompt, model=None, provider=None, toolsets=None)`
- `src/mac/_hermes/hermes_cli/oneshot.py:245` — `def _run_agent(prompt, model=None, provider=None, toolsets=None, use_config_toolsets=True)`

Isolation is unconditional at the call site, not opt-in via a parameter or CLI
flag. The prior repair's parameter approach genuinely did not land; the shipped
outcome is nonetheless isolated.

### 3. Targeted characterization run

Command: `.venv/bin/pytest tests/test_hermes_oneshot_session_isolation.py -v`

Result: **7 passed, 2 skipped, 0 failed** — but several of the passing/skipped
tests encode a PRE-fix (non-isolated) contract and pass only incidentally, so
the green result masks the stale contract rather than validating current
behavior.

## The actionable gap: `tests/test_hermes_oneshot_session_isolation.py` is stale and self-contradictory

The file mixes assertions written against the PRE-fix (non-isolated) code with
assertions written against the POST-fix (isolated) code. Exact functions and
file:line references for the follow-up repair:

### Stale PRE-fix assertions (no longer match the shipped isolation)

- `tests/test_hermes_oneshot_session_isolation.py:129`
  `test_run_oneshot_signature_has_no_skip_memory_param` — asserts
  `"skip_memory" not in params` of `_run_agent`. Passes only because isolation
  was implemented at the call site (kwargs), not as a signature parameter; the
  test's own message ("regression may be fixed; update this characterization
  test") admits it is stale. Encodes the PRE-fix "no isolation knob" contract.
- `tests/test_hermes_oneshot_session_isolation.py:201`
  `test_interactive_cli_has_skip_memory_flag_but_oneshot_does_not` — the name
  and docstring assert oneshot does NOT wire `skip_memory`, but the body
  asserts `"skip_memory" in oneshot_source` (POST-fix). Name/intent contradict
  the assertion; the "asymmetry" it documents no longer exists.
- `tests/test_hermes_oneshot_session_isolation.py:351`
  `test_memory_store_load_from_disk_reads_hermes_home` — docstring asserts that
  "because oneshot._run_agent() does not pass skip_memory=True" persistent
  memory is loaded, and asserts the seeded "should NOT appear in an isolated
  oneshot run" entry IS present. This is the PRE-fix expectation; the shipped
  code skips memory, so the premise is false. (Currently reported skipped in
  the run due to the test's internal import/precondition guard, but its
  assertion still encodes the wrong contract.)
- `tests/test_hermes_oneshot_session_isolation.py:380`
  `test_oneshot_system_prompt_contains_persistent_memory` — asserts persistent
  memory entries appear in the oneshot system prompt "(as it would be when
  skip_memory=False)". PRE-fix expectation, contradicted by the shipped
  `skip_memory=True`. (Also reported skipped for the same guard reason.)
- `tests/test_hermes_oneshot_session_isolation.py:438`
  `test_parser_exposes_no_isolated_flag_for_oneshot` — asserts no
  `--isolated`/`--no-memory`/`--fresh`/`--clean` flag exists. Consistent with
  the shipped design (isolation is unconditional, not flag-gated), but it is
  filed under a class whose docstring says "these tests verify the bug IS
  present ... after patching oneshot.py ... flip the assertions", so its intent
  is inverted relative to the shipped state and it should be re-scoped as a
  positive "one-shot is always isolated, no opt-in flag" contract.

### POST-fix assertions (already match the shipped isolation)

- `tests/test_hermes_oneshot_session_isolation.py:149`
  `test_run_oneshot_aiagent_call_missing_skip_memory` — INTERNALLY
  CONTRADICTORY: the name and docstring say `skip_memory`/`skip_context_files`
  are "NOT in the call", but the body asserts `"skip_memory" in kwarg_names`
  and `"skip_context_files" in kwarg_names` (POST-fix). Assertion is correct;
  name and docstring are stale and must be renamed/rewritten.
- `tests/test_hermes_oneshot_session_isolation.py:286`
  `test_oneshot_agent_loads_persistent_memory_entries` — despite a name/class
  docstring that describe loading persistent memory (PRE-fix), the body asserts
  `skip_memory is True` and `skip_context_files is True` (POST-fix). Assertion
  is correct; name, docstring, and enclosing class comment
  (`tests/test_hermes_oneshot_session_isolation.py:277` — "These tests CONFIRM
  the regression is present and will FAIL once the fix is applied") are stale.

### Additional stale scaffolding to update

- Module docstring `tests/test_hermes_oneshot_session_isolation.py:6` onward,
  and the class banners at
  `tests/test_hermes_oneshot_session_isolation.py:124`,
  `tests/test_hermes_oneshot_session_isolation.py:277`, and
  `tests/test_hermes_oneshot_session_isolation.py:431` still describe the bug as
  present and instruct a future author to "invert the assertions". The fix has
  landed, so these banners are misleading and should be rewritten to describe an
  isolation-holds contract.

## Verdict

The finding is NOT actionable as a production-code repair: `hermes -z` one-shot
isolation is already shipped and correct. It IS actionable as a test-hygiene
repair: `tests/test_hermes_oneshot_session_isolation.py` must be de-duplicated
and de-contradicted so every assertion, name, docstring, and class banner states
the POST-fix contract (one-shot always runs isolated; `skip_memory=True` and
`skip_context_files=True`; no opt-in flag; no persistent memory in the prompt).
The exact function/line list above fully scopes that follow-up. This assessment
made no changes to production `oneshot.py`.
