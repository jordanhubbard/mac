---
id: python-runtime-01
status: open
deps: []
links: [dispatch-toolchain-01, gate-testdeps-01]
created: 2026-06-06T00:00:00Z
type: bug
priority: 2
audit: taskbrain-build-test
discovered_via: autonomous_build_run
---
# Agent build runtime must satisfy a project's requires-python (gke hub ships Python 3.10)

## Why this exists

The taskbrain contract gate runs `python3 -m venv … && pip install -e . …`. On the
`jordanh-gke` **hub pod**, `python3` is **3.10.12**, but taskbrain (like most
modern Python projects) declares `requires-python>=3.11`, so the editable install
fails outright — `ERROR: Package 'taskbrain' requires a different Python: 3.10.12
not in '>=3.11'` — before tests can even run. Every build task the hub agent
claims dies on this; the worker pods (Python >=3.11) succeed.

The sharp part: **a compatible interpreter already exists on the hub** — the mac
venv is **Python 3.12.13** — but the gate uses the system `python3` (3.10), not a
>=3.11 interpreter. So this is both an environment-provisioning gap *and* an
interpreter-selection gap, and it's why builds only succeed on the workers.

## Acceptance Criteria

A build agent should run a project's tests under a Python that satisfies the
project's `requires-python`, with no human intervention:

1. **Interpreter selection.** When creating the test venv, select the highest
   available interpreter that satisfies the project's `requires-python` (prefer
   `python3.12`/`python3.11` over a too-old `python3`), or bootstrap from a
   known-good >=3.11 interpreter (mac's own venv python is >=3.11 by mac's floor).
   Fail with a clear "no compatible interpreter" message if none qualifies.
2. **Pod provisioning.** The deploy should ensure agent pods that build code ship
   a `python3` >= the projects they build; the gke hub pod currently ships 3.10.
3. Complements [[dispatch-toolchain-01]] (don't route a task to an agent whose
   runtime can't satisfy it) and [[gate-testdeps-01]] (install declared test deps)
   — together: the autonomous loop should never stall on a wrong/missing runtime.

## Notes

- Live workaround: the ~30-min monitor requeues hub-failed tasks so they re-land
  on >=3.11 workers; the build proceeds, just with wasted hub dispatch slots.
- Observed: gke hub `python3 --version` = 3.10.12; `~/.mac/venv` python = 3.12.13;
  worker pods >= 3.11.
- Verification: run a `requires-python>=3.11` project's gate on an agent whose
  default `python3` is 3.10 but which has a >=3.11 interpreter available; the gate
  should pick the >=3.11 interpreter and pass.
