# Contract gate environment repair resolution

This closes the investigation recorded by task
`task_92acb364fc564112a06112e08df0eb52` for the contract gate that blocked
`task_8454149df64b4d3f83905115708a4fed`.

## Resolution

The root cause was Git 2.34.1 on the worker image. The merge gate uses
`git merge-tree --write-tree`, which requires Git 2.38 or newer. The resulting
merge-test failures also prevented the serial test slice from running and made
the partial coverage total look like an independent coverage regression.

The repair siblings addressed each part of that failure:

- `git_prereq` makes `scripts/run-contract-tests.sh` exit before pytest with a
  distinct, actionable prerequisite error when no Git >= 2.38 is available.
- `host_image_git` declares Git 2.38 as a repository prerequisite and enforces
  it in fleet onboarding and worker/container images.
- `coverage_margin` labels coverage from a failed test run as partial rather
  than presenting it as a complete gate measurement.

Focused acceptance on Git 2.55.0 passed all 107 tests in
`tests/test_git_toolchain_floor.py`, `tests/test_fleet_prerequisite_receipts.py`,
`tests/test_merge_queue.py`, `tests/test_auto_land.py`, and
`tests/test_predispatch_conflict.py`. The staged Git 2.34.1 runner contract
confirms exit status 3, an error naming the detected and required versions and
the supported override, and no pytest invocation. The parent's absent
`attempt_hardware_context` module is not imported or referenced by the repaired
runner or its prerequisite tests; the repair therefore does not depend on the
parent deliverable.

The authoritative complete contract gate and its final `coverage safety:` line
remain owned by the deterministic host finalizer for this task.
