# Test portfolio strategy

MAC optimizes its tests for defects detected per unit of time and maintenance,
not for test count or aggregate line coverage. Coverage is a diagnostic and a
safety floor. It is not a product KPI, and an uncovered line is not by itself a
reason to create a test.

## Parallel PostgreSQL isolation

Parallel pytest runs automatically provision one PostgreSQL database per xdist
worker before test collection. Tests still receive fresh, isolated schemas
inside their worker database. This avoids serializing unrelated workers on the
production migration advisory lock; the production lock and migration code are
unchanged. Serial pytest invocations continue to use the configured test database.

`MAC_TEST_PG_URL` must point to a test server whose role can create databases.
The repository's local PostgreSQL helper and CI PostgreSQL service provide that
role. If a custom role lacks `CREATEDB`, the parallel run fails with an explicit
prerequisite error rather than silently falling back to the contended database.
Connection settings are preserved when selecting the worker database; subprocesses
inherit the worker's DSN.

The controller records ownership in database comments and holds an advisory
lease in the original test database. Workers hold that lease too, so controller
failure alone does not make their databases eligible for cleanup. Normal teardown
drops only the databases created by that controller. A later parallel run can
reap marked leftovers from the same base database and database owner only after
obtaining an exclusive lease. Unmarked databases are never eligible, and cleanup
never forces existing clients to disconnect. Failed cleanup retains the database
and emits a warning for investigation.

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
durations, outcomes, and per-test coverage contexts, and rebuilds
`src/mac/data/test_impact_map.json` from that run (`MAC_TEST_REBUILD_MAP=1`).
The portfolio report identifies tests with no unique executed lines or arcs.
That is a review queue, not an automatic deletion list: assertions can differ
even when execution is the same. Deleting a test without regenerating the
map strands interned node ids; `make impact-map IMPACT_MAP_ARGS=--write`
prunes those without another coverage run. `make sanity-test` depends on
`make impact-map`, which checks collectability.

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

## Up-front validation

CI has no nightly test schedule. Every pull request and main push runs the
complete primary-Python contract suite with statement, branch, and subprocess
coverage, historical-fault replay, and container contracts. The affected-test
sanity slice remains a fast diagnostic; it does not replace full candidate
validation. Compatibility checks use the pinned fleet Python baseline.

The documentation workflow runs its executable book on Linux and macOS,
generated-reference and strict-HTML checks, live Kubernetes validation, and a
locally built ARM64 candidate image on pull requests and pushes. Building that
candidate directly avoids depending on a concurrent image publication.

Deployable image publication and the OpenShell runtime's tested marker depend
on the full candidate and container gates. Documentation publication depends
on its live boundaries as well as the book and HTML gates. Portfolio analysis
runs on main pushes or explicit dispatch; it has no timer. Explicit workflow
dispatch remains available to revalidate a candidate.

The full suite remains the fail-closed fallback for affected-test selection
that is unavailable, empty, or uncertain. Up-front validation preserves the
coverage floors and does not turn formerly deferred checks into skips.

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

### Preferred replacement direction

When rationalizing the portfolio, prefer integration tests, vertical-slice
tests, and smoke tests that exercise real feature boundaries (process/CLI/API/
contract seams) over narrow unit tests with little incremental value.
Consolidation should land as boundary-exercising tests, not as preserved
statement-for-statement unit coverage.

- A narrow unit test is a removal candidate only when an integration,
  vertical-slice, or smoke test provides an equally strong oracle for the
  same behavior at a real feature boundary; otherwise keep the unit test.
- Rationalization changes replace narrow unit cases with boundary-level
  cases and cite the oracle equivalence, rather than deleting outright.
- The evidence bar is unchanged: the portfolio report must show fault
  detection kept or improved while maintenance size and wall time fall.

## Test-suite rationalization backlog

The first tranche of contract-gate cost reduction folded the 15 `_edges`
coverage-companion modules that had a same-named base twin into their base
modules, relocating every case verbatim so that no exercised line or arc was
lost (verified by branch-mode coverage diff: zero source lines and zero source
arcs dropped across 230 source files, 463 collected cases preserved). The
companions were deleted and the base modules absorbed their imports and helpers
(colliding helpers were renamed with an `_edges` suffix rather than dropped).

Remaining follow-up work, to be filed as tasks against this program.
Each item follows the preferred replacement direction above: land the
consolidation as boundary-exercising tests with cited oracle equivalence,
not as a statement-for-statement move of narrow unit coverage.

- **Split `tests/test_control_plane.py` (14,278+ LOC) along service boundaries.**
  Track the ControlPlane decomposition happening in the sibling task and mirror
  its new service seams as separate `test_control_plane_<service>.py` modules so
  the monolith stops forcing a full-suite collection on every control-plane
  change. Prefer per-seam integration/vertical-slice cases that drive each
  service through its public boundary; retain a narrow unit case only when no
  boundary-level test gives an equally strong oracle for that behavior.
- **Handle the remaining non-twin `_edges` / `_edge_coverage` /
  `_boundary_coverage` companions.** ~22 companions have no clean same-named base
  twin; each needs a target base module (existing or new) chosen before folding,
  or promotion to a first-class behavioral module when it covers a distinct seam.
  Where a companion only re-executes lines a boundary test already covers, fold
  it into that boundary-level oracle and cite the equivalence; promote it only
  when it protects a distinct seam no integration/vertical-slice/smoke test
  already exercises.
