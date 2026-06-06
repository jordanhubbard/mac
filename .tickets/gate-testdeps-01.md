---
id: gate-testdeps-01
status: open
deps: []
links: [repo-onboard-01]
created: 2026-06-06T00:00:00Z
type: bug
priority: 1
audit: taskbrain-build-test
discovered_via: autonomous_build_run
---
# Verification gate can't self-heal a missing/declared test dependency

## Why this exists

During the live autonomous build of `NVIDIA-dev/taskbrain` on the `jordanh-gke`
fleet, the build stalled: every new task started failing the contract gate with
`RuntimeError: starlette.testclient requires the httpx package` (TestClient tests
couldn't collect). The dependency **was correctly declared** by the build agent
in `pyproject.toml` under `[project.optional-dependencies].dev` (`httpx`, `pytest`,
`ruff`, `black`) — but the verification gate's test command
(`pip install -e . pytest`) installed the **main** deps + pytest and **not the
`dev` extra**, so httpx was absent and collection failed. A human had to edit the
contract `test.command` to `pip install -e '.[dev]'` and bulk-update the pending
tasks to unblock the run.

**The defect:** MAC's deterministic verification gate runs the repository
contract's `test.command` **verbatim** in a throwaway venv
(`run_deterministic_git_finalizer`, `src/mac/task_executor.py`). It does not:
- install the project's **declared** dev/test extras, nor
- run the contract's `bootstrap.command` before the test, nor
- recover from a "test dependency missing at collection" signal.

So a declared-but-not-installed test dep blocks the whole autonomous loop and
needs manual operator intervention — exactly what an autonomous fleet should not
require. (The build agent installing deps during its *build* phase doesn't help:
the finalizer's gate uses a separate, fresh venv.)

## Acceptance Criteria

The gate should make declared test/dev dependencies available without a human
editing the contract. Any one of these (in rough preference order):

1. **Finalizer owns the test env + installs declared extras.** When the repo is a
   Python project, the finalizer builds the test venv itself and installs the
   declared `dev`/`test` optional-dependency extras (e.g.
   `pip install -e '.[dev]'` / `'.[test]'`, falling back to `-e .`) before running
   the (narrower) test command — instead of executing an opaque operator string
   that bakes its own venv + install.
2. **Run `contract.bootstrap.command` before `test.command`.** Today the finalizer
   only runs `test.command`; the contract already has a `bootstrap` field that
   should own environment setup (incl. extras) and run first.
3. **Self-heal retry.** If `test.command` fails and the output matches a missing
   test-dependency signal (`ModuleNotFoundError` / "No module named" / "requires
   the X package to be installed") at collection, install the missing module (or
   the declared dev/test extras) into the test env and retry once; record the
   install as `verification.environment_delta` (the existing proposed-runtime-delta
   mechanism) for operator visibility.
4. **Onboarding authors a correct test command.** Strengthen the onboarding
   prompt (`task_executor.repository_contract_section`, the onboarding branch) so
   the agent's authored `.mac/project.yaml` test command installs dev/test extras
   — reducing the chance an operator hand-rolls a wrong one.

## Notes

- Live workaround already applied for the taskbrain run: contract `test.command`
  updated to `rm -rf /tmp/tbv && python3 -m venv /tmp/tbv && /tmp/tbv/bin/pip
  install -q -e '.[dev]' && /tmp/tbv/bin/python -m pytest -q` on all pending tasks
  (clean venv + dev extra). Verified green on `main`.
- Not hot-patched mid-build on purpose: the finalizer runs in the worker
  executors; redeploying mid-run would interrupt in-flight tasks. This is an
  architectural change (the finalizer owning the test env) best done deliberately,
  then deployed between runs.
- Related, separate gap seen in the same run: the **hub agent** (`jordanh-gke`,
  Python 3.10) fails the `requires-python>=3.11` install for tasks it claims, so
  build tasks must land on the >=3.11 workers; consider keeping the hub off build
  duty or giving the hub pod >=3.11. Track separately if it recurs.
- Verification: re-run a repo whose tests need a `dev`-only dependency (e.g.
  FastAPI `TestClient`/httpx) through a contract with NO dev-extra install in its
  command; the gate should still pass by auto-installing the declared extra.
