# Test portfolio strategy

MAC optimizes its tests for defects detected per unit of time and maintenance,
not for test count or aggregate line coverage. Coverage is a diagnostic and a
safety floor. It is not a product KPI, and an uncovered line is not by itself a
reason to create a test.

## What justifies a test

Every durable test must protect at least one of these:

- a public API, CLI, persistence, protocol, or artifact contract;
- a security, authorization, isolation, publication, or recovery invariant;
- a regression that has occurred in production or a realistic fault that the
  test demonstrably detects;
- a state-machine transition or property that covers a meaningful family of
  cases; or
- a boundary between processes, hosts, sandboxes, databases, or external
  services.

Tests added only to execute uncovered statements are not acceptable. Prefer
deleting unreachable code, reducing unnecessary branches, or consolidating
equivalent cases. A line-coverage increase is useful only when it follows from
better behavioral or fault-detection evidence.

## Evidence used to maintain the portfolio

The canonical coverage run measures statements, branches, and Python child
processes. `make test-portfolio` additionally records exact pytest node IDs,
durations, outcomes, and per-test coverage contexts. The portfolio report
identifies tests with no unique executed lines or arcs. That is a review queue,
not an automatic deletion list: assertions can differ even when execution is
the same.

A test is a safe deletion or consolidation candidate only when all applicable
evidence agrees:

1. it protects no distinct requirement or historical regression;
2. it contributes no unique branch or process-boundary behavior;
3. removing it does not reduce historical-fault or mutation detection;
4. another mandatory test has an equally strong oracle for the behavior; and
5. it is not retained as a materially faster, more diagnostic failure signal.

Historical-fault probes are intentionally separate from pytest case count.
`make fault-replay` runs each probe against the fixed tree and the pre-fix tree:
the probe must pass now and fail before the fix. This proves that the portfolio
detects a real fault rather than merely executing code.

## Test layers

- **Focused tests** isolate algorithms, validation rules, and state-machine
  invariants. Prefer property or table-driven coverage over one function per
  syntactic branch.
- **Contract tests** exercise one public boundary and its serialization,
  authorization, error, or persistence semantics.
- **Process integration tests** run real MAC processes and communicate through
  public HTTP/CLI/protocol surfaces. They may replace focused happy-path tests
  when they detect the same faults with an equally precise oracle.
- **Black-box end-to-end tests** start deployable artifacts and use only public
  interfaces. Importing or calling a private MAC function inside a container is
  a container contract test, not an end-to-end test.

Duplicating a happy path at every layer is not required. Lower-level tests stay
only for distinct edge behavior, fault localization, or a faster required
signal. A black-box health check does not replace a focused semantic assertion;
a black-box workflow that detects the same injected fault can.

## Pipeline tiers

The tiers avoid running the same test twice in one job:

1. **Sanity / pull request** runs deterministic public-contract canaries, true
   process E2E seams, directly changed tests, and CodeGraph-affected tests. If
   affected selection is unavailable or untrustworthy, it falls back to the
   full contract gate.
2. **Mainline publication** runs the full statement, branch, and subprocess
   coverage gate once on the primary Python version.
3. **Compatibility** runs import, CLI/API contract, and process-E2E smoke on
   the secondary Python version. The full secondary-version matrix runs on the
   schedule below rather than duplicating every pull-request test.
4. **Nightly / explicit audit** runs the complete version matrix, live optional
   backends when configured, container contracts, portfolio analysis, and
   historical-fault replay.

The full suite remains the fail-closed fallback for changes to test selection,
coverage configuration, dependency/bootstrap/runtime files, shared test
infrastructure, or any change for which affected-test selection is empty or
uncertain.

## Coverage policy

Coverage policy is checked by `scripts/coverage-policy.py` from machine-readable
coverage JSON. It has separate statement and branch safety floors so a high
statement percentage cannot hide untested decisions. The values prevent a
large accidental loss of exercised behavior; they are not targets to maximize.

Changing a floor requires a test-portfolio report and an explanation of the
behavior gained or lost. The preferred responses to a floor failure are, in
order: restore a lost behavioral test, remove dead code, simplify the decision
structure, or add a requirement-driven test. Adding assertions solely to move
the percentage is prohibited.

## Portfolio review metrics

Portfolio changes report both before and after values:

- historical faults detected and, when sampled, mutants killed;
- covered and missing statements and branches;
- unique lines/arcs per test context;
- full and sanity wall time;
- test functions, executed cases, and test physical lines;
- skipped and flaky cases; and
- which public contracts and E2E seams ran.

A successful rationalization keeps or improves fault and contract detection
while reducing maintenance size, duplicate execution, or wall time. It is
acceptable for raw test count or aggregate line coverage to stay flat or fall
when stronger evidence shows the portfolio is at least as effective.

## Test-suite rationalization backlog

The first tranche of contract-gate cost reduction folded the 15 `_edges`
coverage-companion modules that had a same-named base twin into their base
modules, relocating every case verbatim so that no exercised line or arc was
lost (verified by branch-mode coverage diff: zero source lines and zero source
arcs dropped across 230 source files, 463 collected cases preserved). The
companions were deleted and the base modules absorbed their imports and helpers
(colliding helpers were renamed with an `_edges` suffix rather than dropped).

Remaining follow-up work, to be filed as tasks against this program:

- **Split `tests/test_control_plane.py` (14,278+ LOC) along service boundaries.**
  Track the ControlPlane decomposition happening in the sibling task and mirror
  its new service seams as separate `test_control_plane_<service>.py` modules so
  the monolith stops forcing a full-suite collection on every control-plane
  change.
- **Handle the remaining non-twin `_edges` / `_edge_coverage` /
  `_boundary_coverage` companions.** ~22 companions have no clean same-named base
  twin; each needs a target base module (existing or new) chosen before folding,
  or promotion to a first-class behavioral module when it covers a distinct seam.
