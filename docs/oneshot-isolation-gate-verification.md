# Oneshot isolation — contract gate verification

Verification record for the `hermes -z` one-shot session-isolation area after
the stale characterization tests were reconciled with the applied fix. This
document captures the operational evidence that the repository contract gate is
green for this area, complementing the automated regression module
`tests/test_hermes_oneshot_session_isolation.py`.

## Scope

The one-shot path (`hermes -z`) must run in a clean, isolated state instead of
inheriting persistent session context. The applied fix lives in
`src/mac/_hermes/hermes_cli/oneshot.py`: `_run_agent()` constructs `AIAgent(...)`
with `skip_memory=True` and `skip_context_files=True`, so a one-shot run never
loads persistent `MEMORY.md` / `USER.md` or repo context files. The session
store wiring is unchanged (`_create_session_db_for_oneshot()` still opens the
default `state.db` under `HERMES_HOME`); isolation is enforced by the agent not
requesting persistent memory, not by repointing the store.

## What was verified

| Check | How | Result |
|---|---|---|
| Reconciled module (bare) | `pytest tests/test_hermes_oneshot_session_isolation.py` | 9 passed |
| Reconciled module (hermetic gate) | `scripts/run-contract-tests.sh tests/...isolation.py -v` | 9 passed, 0 skipped |
| Full contract gate | `scripts/run-contract-tests.sh` | green, exit 0 |
| Coverage floors | statement/branch floor policy | met |

The hermetic gate scrubs `HERMES_*` / `MAC_* `/ `GH_TOKEN` and redirects `HOME`
to a throwaway directory, so passing there — not just under a bare `pytest` — is
the meaningful signal. The reconciled module reports no skips under the gate:
the runtime `TestOneshotMemoryIsolation` class is guarded by a `SNAPSHOT_PIN`
`skipif` that correctly runs when the vendored Hermes snapshot is present.

## Gate summary

```text
bulk phase:      8667 passed, 4 skipped
protected phase: 4 passed, 39 skipped, 8671 deselected
coverage:        statements 90.88% (floor 90.00%), branches 80.26% (floor 80.00%)
```

No failures were attributable to the reconciled test module or the one-shot
isolation area. No source change to the fix was required — it is already applied
in `src/mac/_hermes/hermes_cli/oneshot.py` — so this record and the reconciled
regression module are the tracked deliverables for the gate.
