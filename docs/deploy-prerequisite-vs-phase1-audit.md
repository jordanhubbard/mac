# Audit: prove deploy prerequisites before phase-1 mutation, preserve Python diagnostics

Investigation-only ground truth for the repair task
"Prove all deploy prerequisites before phase-1 mutation and preserve Python
diagnostics". No production behavior is changed by this document; it specifies
the exact edits and test assertions the follow-on repair must make.

## Finding A — phase-1 worker mutation precedes prerequisite proof

In `deploy/deploy-mac-fleet.sh`, `run_typed_cohort()` runs the phases in this
order:

1. `run_bounded_node_phase "$selected_specs_file" phase1-prepare
   typed_phase1_prepare_worker`
2. `run_bounded_node_phase "$selected_specs_file" prerequisites
   typed_prerequisite_worker`
3. `run_bounded_node_phase "$selected_specs_file" stage-bundle ...`

`typed_phase1_prepare_worker` calls `prepare_remote_phase1_restore_contract`,
which `acquire_remote_deployment_lock`, uploads the phase-1 quiesce helper /
daemon-functions files to the worker, and runs `bash "$helper" prepare` on the
remote worker. That is a real per-worker mutation (deployment lock acquisition
plus remote helper execution that arms the restore contract).

`typed_prerequisite_worker` (`prepare_remote_prerequisite_bundle`) is the
read-only prerequisite proof. It runs AFTER the phase-1 prepare mutation.

Therefore phase-1 mutation currently begins before all read-only prerequisites
are proven, violating the requirement to "prove all deploy prerequisites before
phase-1 mutation".

Additionally, `hub_dispatch_hold_transition_available()` (deploy-mac-fleet.sh
~line 3406) is defined — the comment at ~line 3409 states the intent to "prove
the POST operation itself before phase 1 mutates any worker" — but it has NO
call site anywhere in the script. `hub_dispatch_hold_cas_available()` is the
only availability gate actually invoked (in `prepare_remote_mac_agent_deployment`).
So the transition-batch prerequisite is never proven before phase-1 mutation.

### Required source edits (Finding A)
- Invoke the read-only prerequisite proof before any phase-1 mutation. In
  `run_typed_cohort()`, move the `prerequisites` bounded phase (and the
  never-invoked `hub_dispatch_hold_transition_available` gate) ahead of the
  `phase1-prepare` bounded phase / journal `phase1-prepare-start` mutation so
  that read-only prerequisite proof and the transition-batch POST availability
  check both complete before `typed_phase1_prepare_worker` acquires any remote
  deployment lock or runs the remote `prepare` helper.
- Wire `hub_dispatch_hold_transition_available` into the pre-phase-1 gate
  (mirroring how `hub_dispatch_hold_cas_available` is checked), failing closed
  when the hub only exposes `release-batch` and lacks the distinct
  `POST /agents/dispatch-hold/transition-batch` operation.
- Keep `assert_prerequisite_remaining_budget` (using
  `MAC_DEPLOY_PREREQUISITE_PHASE_BUDGET_SECONDS`, default 2400, and
  `MAC_DEPLOY_PREREQUISITE_APPLY_GUARD_SECONDS`, default 120) applied so the
  reordered prerequisite phase still enforces the remaining-time budget before
  mutation.

### Required test edits (Finding A)
- `tests/test_deploy_fleet_parallel_staging.py::test_parallel_typed_barriers_keep_wal_parent_owned_and_ordered`
  currently codifies the buggy order: its `ordered` tuple places
  `run_bounded_node_phase ... phase1-prepare` and `cohort_journal_mutate
  phase1-armed` BEFORE `run_bounded_node_phase ... prerequisites`. Update this
  tuple so `prerequisites` precedes `phase1-prepare`/`phase1-prepare-start`, and
  keep the `positions == sorted(positions)` assertion.
- Add an assertion (here or in `tests/test_deploy_hold_adoptions.py`) that
  `hub_dispatch_hold_transition_available` has a real call site in
  `run_typed_cohort`/`main` and that its position precedes the first phase-1
  mutation (`phase1-prepare-start` journal write / `typed_phase1_prepare_worker`).
- `tests/test_deploy_hold_adoptions.py` currently only asserts the availability
  helper's body inspects `/agents/dispatch-hold/transition-batch` and its
  `post`; extend it to assert the helper is invoked before phase-1 mutation.

## Finding B — Python helper diagnostics swallowed on unexpected failure

`deploy/fleet-node-phase1-quiesce.sh` embeds Python blocks whose top-level
`except Exception` handlers discard the exception message and traceback,
printing only the class name:

- ~line 2952:
  `print("phase-1 quiescence failed unexpectedly: %s" % type(exc).__name__, ...)`
- ~line 3744:
  `print("phase-1 quiescence failed unexpectedly: %s" % type(exc).__name__, ...)`

On an unexpected failure an operator sees e.g. "phase-1 quiescence failed
unexpectedly: KeyError" with no message and no traceback, making remote
diagnosis impossible. The curated `QuiescenceFailure` / `ReceiptFailure`
branches already print a useful message and must stay; only the unexpected
fallback loses information.

Note: the raw child-process (systemctl / launchctl / manager) stderr captured by
`run_bounded()` is intentionally suppressed to avoid leaking secret-bearing
command output (see `reject_secret_or_raw_output` and the
`test_systemd_inspection_error_fails_closed_without_raw_output` contract). The
fix must preserve the Python-level diagnostic (message + `traceback.format_exc()`
of the helper itself) WITHOUT reintroducing raw child stderr.

### Required source edits (Finding B)
- In both `except Exception as exc:` fallbacks, print the exception message and a
  full Python traceback (e.g. `traceback.format_exc()`), not just
  `type(exc).__name__`, to stderr before `raise SystemExit(1)`. Add
  `import traceback` where needed. Keep the existing best-effort
  `output_path.unlink()` cleanup in the receipt block.
- Do NOT surface raw child-process stdout/stderr; only the helper's own Python
  message/traceback.

### Required test edits (Finding B)
- Add a test in `tests/test_fleet_node_phase1_quiesce.py` that forces an
  unexpected (non-`QuiescenceFailure`) exception in each Python block and asserts
  the helper stderr contains the exception message and traceback frames (e.g.
  the exception type AND a `Traceback (most recent call last)` line and the
  helper file/function), while still asserting no secret / raw child output
  leaks (mirroring `test_systemd_inspection_error_fails_closed_without_raw_output`).

## Verification
- Canonical command: `scripts/run-contract-tests.sh`.
- Targeted while iterating:
  `.venv/bin/python -m pytest tests/test_deploy_fleet_parallel_staging.py
  tests/test_deploy_hold_adoptions.py tests/test_fleet_node_phase1_quiesce.py -q`.

## Out of scope / not implicated
- `src/mac/diagnostics.py` is the read-only control-plane diagnostics registry
  (`mac admin diagnostics`); it already isolates a raising check into an `error`
  Finding and is unrelated to the phase-1 helper stderr loss. No change needed.
- `src/mac/deploy_service.py` records deploy/dependency metadata; it does not run
  the phase-1 helper or handle its stderr. No change needed.
