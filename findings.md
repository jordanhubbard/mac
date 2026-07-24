# Ground-truth audit: openclaw_fleet_rollout contract gate

Scope: read-only investigation for the contract-repair of parent
task_8d11d7df897c4e48a2efed76f79b1bc6 (contract `.mac/project.yaml`).
No edits to `src/`, `tests/`, `skills/`, or `deploy/`.

## 1. Bootstrap
`python3 scripts/bootstrap-project.py` exits 0 and provisions the declared
binaries:
- `.venv/bin/python` (symlink -> python3.12)
- `.venv/bin/pytest`
- `.venv/bin/coverage`

## 2. Public API + tests
`src/mac/openclaw_fleet_rollout.py` exposes the full required public API:
- `ROLLOUT_PLAN_SCHEMA` = `"mac.openclaw_fleet_rollout.v1"`
- `RolloutPlanStep`, `RolloutPlan`, `RolloutResult` (dataclasses)
- `build_staged_rollout_plan(...)`, `execute_staged_rollout(...)`

`tests/test_openclaw_fleet_rollout.py` imports and exercises the API. It
imports `RolloutPlan`, `RolloutPlanStep`, `RolloutResult`,
`build_staged_rollout_plan`, and `execute_staged_rollout` directly, and a
symbol-presence guard test loads the module (`import mac.openclaw_fleet_rollout
as module`) to assert all five names plus their callability/type. The module
also defines `ROLLOUT_PLAN_SCHEMA`, completing the six-symbol surface.

## 3. Canonical contract gate
`scripts/run-contract-tests.sh tests/test_openclaw_fleet_rollout.py -q`:
- Result: **52 passed**
- Exit code: **0** (green)

## 4. Git ground truth
- `git status --porcelain`: empty (working tree clean).
- Both deliverables are tracked in HEAD (`git ls-files --error-unmatch` succeeds).
- Both files are present in the baseline commit `28fa1fb "MAC OpenShell sandbox baseline"`.

## Conclusion
The module and tests exist and are already committed in the baseline; the
contract gate is green (52/52, exit 0); the working tree is clean. No
source/commit/push work remains for the deliverables. The repair child has
nothing to fix in the module/tests — the prerequisite is already satisfied.
