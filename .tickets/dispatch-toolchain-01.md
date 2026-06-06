---
id: dispatch-toolchain-01
status: open
deps: []
links: [gate-testdeps-01]
created: 2026-06-06T00:00:00Z
type: bug
priority: 2
audit: taskbrain-build-test
discovered_via: autonomous_build_run
---
# Dispatch isn't toolchain-aware; the hub agent claims build tasks it can't run

## Why this exists

During the autonomous `NVIDIA-dev/taskbrain` build on `jordanh-gke`, the hub
agent `jordanh-gke` repeatedly claimed repo-build tasks and **failed** them: the
hub pod runs **Python 3.10.12**, but taskbrain declares `requires-python>=3.11`,
so `pip install -e .` in the contract gate fails before tests can run. The
two worker pods run Python >=3.11 and complete the same tasks fine. The monitor
has to requeue every hub-claimed failure so it re-lands on a worker — wasted
dispatch slots, churn, and a slower build.

Two distinct gaps under this:

1. **Dispatch ignores task toolchain vs agent runtime.** A task's
   `origin.repository_contract.toolchain` (and effectively its required Python
   version) is not matched against the claiming agent's actual runtime, so an
   agent that *cannot* satisfy the toolchain still claims the task and then fails
   the gate. Agents already self-report capabilities + hardware
   (`resources`/`machine_hardware_satisfies`); runtime/toolchain fit is the
   missing dimension.
2. **The control-plane/hub agent acts as a build executor.** The hub is the
   coordinator + reviewer + shared-services manager; having it also compete for
   build claims (with, here, an incompatible interpreter) is usually wrong.

## Acceptance Criteria

Build tasks should only be claimed by agents that can actually run them, without
an operator/monitor requeuing failures:

1. **Toolchain-aware dispatch.** When a task carries a `repository_contract`
   declaring `toolchain.required_commands` (and a Python/runtime floor), the
   dispatcher only assigns it to agents whose self-reported runtime satisfies it
   — mirroring the existing capability/hardware eligibility used for service-role
   claims and provisioning. Incompatible agents do not claim it.
2. **Hub off build duty (default).** The control-plane/hub agent does not claim
   repo-build tasks by default (review/coordinate only); make build participation
   opt-in per agent role.
3. (Alternative/cheaper) Ensure hub pods that must build are provisioned with the
   required toolchain (e.g. Python >=3.11).

## Notes

- Live workaround for the taskbrain run: the ~30-min monitor requeues any
  hub-failed task (`failed->open`) so it re-lands on a >=3.11 worker; the build
  proceeds, just with wasted hub slots.
- Surfaced alongside [[gate-testdeps-01]] (the gate not installing declared test
  deps); both are "the autonomous loop shouldn't need a human to babysit the
  environment" issues.
- Verification: submit a task whose contract requires a toolchain only some
  agents have; confirm only eligible agents claim it and incompatible agents
  never do (and the hub agent doesn't claim build tasks at all under the default
  role).
