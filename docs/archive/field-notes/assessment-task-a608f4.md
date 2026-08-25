!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Assessment: task_a608f4405b0446a3b28ed7a8beb4fd65

**Task**: Establish ground truth for the dream finding
`verify select-sanity-tests failure pattern actionability` in the mac repository
(contract `.mac/project.yaml`, gate `scripts/run-contract-tests.sh`).
**Origin candidate**: parent task_6e89baae5e074c4789f56eee1aa36ccf
("Repair contract prerequisites: Investigate dream finding: verify
select-sanity-tests failure pattern actionability").
**Finding kind**: dream-cycle investigation finding — scope project — confidence low.
**Repo areas**: `scripts/select-sanity-tests.py`, `scripts/run-sanity-tests.sh`,
`tests/test_select_sanity_tests.py`.
**Assessment date**: 2026-07-16
**Assessed by**: fleet worker (investigation only; no production selector edits).

## Status: CLOSED — the "select-sanity-tests failure pattern" is NOT an actionable correctness defect

`scripts/select-sanity-tests.py` is a correctly functioning, fail-closed test
selector. Every `reason`/`mode` outcome the dream finding flags as a "failure
pattern" is an intentional, safe scope widening (or a safe focused fallback),
not a crash, mis-selection, or silent gap. The finding is a false/low-confidence
signal and should be closed. No production file change is required to fix a
defect, because there is no defect. Two cosmetic, low-severity follow-ups are
noted below but are optional and out of scope for a correctness repair.

## Ground Truth Observed

Measured in the task-owned worktree with the bootstrapped `.venv`
(`python3` 3.12.13; `git`/`gh` present; pytest/coverage from the pre-seeded
`.venv`). Production `select-sanity-tests.py` was read, not modified.

### Reproduction — direct selector runs

`python3 scripts/select-sanity-tests.py --changed-file <path>` (and the module's
`select()` entrypoint) produce:

- `select([])` (truly empty scope) → `mode=full`,
  `reason=no_trustworthy_changed_file_scope`. Fail-closed to the full suite.
- `pyproject.toml` (shared infra) → `mode=full`,
  `reason=test_or_shared_runtime_infrastructure_changed`. Fail-closed.
- `README.md` / non-code, non-broad → `mode=focused`, `reason=non_code_change`,
  `tests=[]` (no executable tests required).
- `src/mac/planning.py` in this worktree → `mode=focused`, with a non-empty
  scope built from module-mapped tests plus the five canary tests.

None of the representative inputs crashed or produced an empty/under-scoped
selection for a real code change. `main()` additionally wraps `select()` in a
`try/except (OSError, RuntimeError)` that fails closed to
`mode=full, reason=selection_error` with the error string attached.

### Reproduction — targeted test module

`scripts/run-contract-tests.sh tests/test_select_sanity_tests.py` → 26 passed.
Line coverage of `scripts/select-sanity-tests.py` under that module alone is
98.20% (only the `print(json.dumps(...))` CLI line and its branch are missed,
and those are exercised by the direct CLI runs above).

### Reproduction — full contract gate

`scripts/run-contract-tests.sh` (no args, the canonical gate) passes cleanly on
this baseline: pytest green, coverage safety floors met
(statements 92.71% ≥ 90.00%; branches 84.22% ≥ 80.00%).

## Actionability Determination

- (a) Does the fail-closed logic ever mis-select or crash on a realistic
  changed-file set? **No.** Every path either widens to `full` or produces a
  canary-backed focused scope; errors fail closed to `full`.
- (b) Do any `reason`/`mode` outcomes surface as an unhelpful "failure pattern"
  lacking actionable diagnostics? **No.** `no_trustworthy_changed_file_scope`
  and `selection_error` carry explicit machine-readable reasons, and
  `selection_error` carries the underlying error. These are diagnostics, not
  defects.
- (c) Is there a coverage gap in `tests/test_select_sanity_tests.py` for the
  suspected pattern? **No meaningful gap.** The module already asserts
  `no_trustworthy_changed_file_scope`, `selection_error`, and the focused
  canary scope.

**Conclusion: NOT ACTIONABLE as a correctness repair. Recommend closing the
dream finding.**

## Closure (repair node)

The dream finding is closed as NOT actionable: `scripts/select-sanity-tests.py`
is a correctly functioning, fail-closed selector with machine-readable
diagnostics, so no production correctness change is warranted. To pin the
intended fail-closed behavior, `tests/test_select_sanity_tests.py` asserts that
the selector emits a canary-backed focused scope for mapped source changes.
