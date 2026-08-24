# Investigation: what actually failed the `scripts/run-contract-tests.sh` gate for task_8454149df64b4d3f83905115708a4fed

Task: `task_92acb364fc564112a06112e08df0eb52` (ground truth child of `task_repair_7e7e1402a65cd673db7bbb97`).
Scope: diagnosis only. **No `src/` or `scripts/` file is changed by this task** — the sibling repair
tasks (`git_prereq`, `host_image_git`, `coverage_margin`) own the fixes.

## Verdict in one paragraph

**(a) is the cause. (b) is a symptom of (a), not an independent blocker.** Running the gate under a
git that lacks `merge-tree --write-tree` adds **42 test failures** and removes **87 branches**
(0.3468 pp) of whole-repo branch coverage. The host's reported branch shortfall was **7 branches**
(0.0260 pp). The coverage lost to (a) is **13.3x** the shortfall that (b) reported, so (a) fully
accounts for (b). Independently, `run-contract-tests.sh` exits with `pytest_status` *before* it
considers `policy_status`, so the host's `exit 1` is attributable to the failing tests regardless of
what the coverage line said. Nothing about the parent task's work
(`src/mac/attempt_hardware_context.py` + tests) is implicated.

## Method and environment

Two whole-suite runs of the declared contract command in this task worktree, identical except for
the `git` on `PATH`:

| | run 1 | run 2 |
| --- | --- | --- |
| `git` | real, 2.39.5 | PATH shim reporting 2.34.1 |
| command | `scripts/run-contract-tests.sh` | `PATH=/tmp/gitshim:$PATH scripts/run-contract-tests.sh` |

Sandbox: 12 cores (`nproc`), `git version 2.39.5`, Python 3.12.7, worktree clean at `42fad2b`
("MAC OpenShell sandbox baseline"). Postgres was provisioned by the gate's own
`scripts/start-test-postgres.sh`.

The shim is a throwaway file in `/tmp`, never installed, never `sudo`, and it touches nothing
outside this process tree. It resolves the subcommand *past* git's global options — an early version
that only inspected `$1` missed every `git -C <dir> merge-tree` call and produced a false negative:

```bash
#!/usr/bin/env bash
REAL_GIT=/usr/bin/git
# ... skip -C/-c/--git-dir/... to find the real subcommand, then:
case "$sub" in
    version)     echo "git version 2.34.1"; exit 0 ;;
    merge-tree)  for a in "$@"; do
                     if [ "$a" = "--write-tree" ]; then
                         echo "usage: git merge-tree [--trivial-merge] <base-tree> <branch1> <branch2>" >&2
                         exit 129
                     fi
                 done ;;
esac
exec "$REAL_GIT" "$@"
```

The shim reproduces the host's stderr line 1 byte-for-byte, which is the first corroboration that it
models the host correctly:

```
run-contract-tests.sh: WARNING: git version 2.34.1 < 2.38 — merge-gate tests (and the production merge queue) WILL fail; upgrade git on this host
```

## Run 1 — real git 2.39.5. The complete failing set

```
= 2 failed, 10938 passed, 3 skipped, 1 xfailed, 58 warnings in 1014.73s (0:16:54) =
```

```
FAILED tests/cli/test_cli_task_update_dependencies.py::test_adding_a_dependency_does_not_re_evaluate_state
FAILED tests/test_dispatch_route_contract.py::test_every_route_the_dispatch_layer_calls_exists
```

Exit status: **1** (from pytest). Coverage policy on the same run:

```
coverage safety: statements 70382/77464 (90.86%, floor 90.00%); branches 20609/25088 (82.15%, floor 80.00%)
```

**Both floors pass, with 0.86 pp and 2.15 pp of margin.** On a `>=2.38` baseline, hypothesis (b)
does not fail the gate on its own.

Neither surviving failure is merge-gate related; both are pre-existing on the unmodified baseline
(the worktree carries no source edits) and neither involves `git`:

- `test_adding_a_dependency_does_not_re_evaluate_state` — a deliberately pinned "KNOWN LIMITATION"
  assertion, `assert after.state != "waiting"`, that now observes `'waiting'`. The behaviour it
  documents has changed; the pin is stale.
- `test_every_route_the_dispatch_layer_calls_exists` — genuine drift: `dispatch.py` calls nine
  routes the hub does not serve (`GET/POST /github-ingest/*`, `/backlog-groom/*`,
  `/model-selection/*`, `/repository-refs/*`).

This second one **passed** in run 2 while failing in run 1, so it is order- or worker-dependent
under `--dist loadscope`, not deterministic. Neither belongs to this task's scope; both are flagged
below for separate triage.

## Run 2 — shimmed git 2.34.1. What (a) costs

```
= 44 failed, 10896 passed, 3 skipped, 1 xfailed, 50 warnings in 956.99s (0:15:56) =
coverage safety: statements 70157/77464 (90.57%, floor 90.00%); branches 20522/25088 (81.80%, floor 80.00%)
```

Exit status: **1**. 42 additional failures, all of them `merge-tree`-dependent, in six files:

| file | failures |
| --- | --- |
| `tests/test_publication_pull_request.py` | 14 |
| `tests/test_native_merge_queue.py` | 6 |
| `tests/test_merge_queue.py` | 6 |
| `tests/test_predispatch_conflict.py` | 6 |
| `tests/test_control_plane.py` (`test_git_publication_*`) | 7 |
| `tests/test_bus_merge_announcement.py` | 3 |
| `tests/test_worker.py::test_review_nudge_prepares_review_worktree_and_git_main_publication` | 1 |

Every one of them fails with the same root error, e.g.:

```
    def test_clean_merge_different_files(repo: Path):
        _branch_commit(repo, "topic", "new_file.txt", "brand new\n")
        verdict = validate_projected_merge(str(repo), "main", "topic")
>       assert verdict.clean is True
E       AssertionError: assert False is True
E        +  where False = MergeGateVerdict(..., error='git merge-tree failed (rc=129): usage: git merge-tree [--trivial-merge] <base-tree> <branch1> <branch2>', merged_tree_sha='').clean
```

`rc=129` matches exactly what the host evidence reported. **Hypothesis (a) is confirmed.**

### Candidates from the brief that are ruled out

Three of the five files named as candidates in the task brief are **not** git-version sensitive.
Under the shim they pass unchanged:

- `tests/test_auto_land.py` — intercepts `argv[0] == "merge-tree"` in a fake runner; no real git.
- `tests/test_hermes_adapter.py` — `"merge-tree"` appears only as memory-record `content_contains`
  search text.
- `tests/test_outcome_grounded_lessons.py` — same; `"git merge-tree needs 2.38"` is a stored lesson
  string, not an invocation.

Isolated confirmation (those five files alone, real git vs shim): `109 passed` → `12 failed, 97
passed`, and the 12 are entirely from `test_merge_queue.py` and `test_predispatch_conflict.py`.

## The branch-coverage delta attributable to the merge-gate tests

Whole-suite, apples to apples (same tree, same selection, same shape of run — only `git` differs):

| | statements | branches |
| --- | --- | --- |
| real git 2.39.5 | 70382 / 77464 (90.86%) | 20609 / 25088 (82.15%) |
| shimmed git 2.34.1 | 70157 / 77464 (90.57%) | 20522 / 25088 (81.80%) |
| **delta attributable to git < 2.38** | **225 statements (0.2905 pp)** | **87 branches (0.3468 pp)** |

Per-file attribution, from an isolated run of the eleven merge-gate-adjacent test files (real git:
`238 passed`; shim: `35 failed, 203 passed`). These are that subset's *unique* losses — larger than
the whole-suite delta because in a full run other tests re-cover some of the same arcs:

```
 +77 branches  +219 stmts  src/mac/services.py
 +31 branches   +71 stmts  src/mac/agentbus_broadcast.py
 +12 branches   +31 stmts  src/mac/merge_queue.py
  +6 branches    +6 stmts  src/mac/predispatch_conflict.py
  +4 branches    +8 stmts  src/mac/gitops.py
  +4 branches    +2 stmts  src/mac/observability_service.py
  +1 branches    +1 stmts  src/mac/api.py
TOTAL +135 branches +338 stmts
```

**The 87-branch / 0.3468 pp whole-suite figure is the one to quote.**

## Does (a) cause (b)? Yes, with a 13x margin

The host reported `branches 21542/26936 (79.97%, floor 80.00%)`. Exactly:

- `21542 / 26936 = 79.9748%`.
- Clearing the 80.00% floor needs `21549` covered branches.
- **Shortfall: 7 branches = 0.0260 pp.**

The measured cost of (a) is 0.3468 pp. Scaled onto the host's 26936-branch denominator that is
**93 branches**, which would have put the host at `21635 / 26936 = 80.32%` — comfortably above the
floor. The coverage lost to the git version is **13.3x** the coverage the host was missing.

There is a **second, smaller way (a) depressed that number**, worth stating precisely because its
size is comparable to the shortfall itself. `run-contract-tests.sh` runs the xdist-safe bulk slice
first and only runs the serial (`process_e2e or postgres or container_contract or docker_e2e`)
phase **if the bulk slice passed**:

```bash
"$PY" -m coverage run -m pytest -n "$_MAC_TEST_JOBS" --dist loadscope \
    -m "not ($_SERIAL_MARK)" || pytest_status=$?
if [ "$pytest_status" -eq 0 ]; then
    "$PY" -m coverage run -m pytest -m "$_SERIAL_MARK" || pytest_status=$?
fi
```

So on the host the merge-gate failures did not merely remove their own coverage — they **suppressed
the entire serial phase**, whose coverage is then absent from the total the policy evaluates. Both
runs above have this same property (each shows a single pytest session summary), which is why they
remain a fair comparison with each other.

Measured, so it is not left as hand-waving: the serial phase run on its own
(`34 passed, 4 skipped, 10944 deselected`) contributes **6 branches and 13 statements** that the
bulk slice does not already cover — **0.0239 pp**. Small in absolute terms, and much smaller than
the 0.3468 pp above, but it is **86% of the host's entire 7-branch shortfall on its own**. So the
suppressed serial phase is not the main story, yet it alone is nearly enough to explain the miss.
(Two caveats, both making this a lower bound: the marginal set is computed against the shimmed bulk
run, and 4 container/docker-marked tests skipped for want of a runtime here.)

And the reported coverage line is not evidence of anything about the tests, because the script
prints it and *then* exits on `pytest_status` first:

```bash
if [ "$pytest_status" -ne 0 ]; then exit "$pytest_status"; fi
...
exit "$policy_status"
```

## Recommendations for the sibling repair tasks

### `git_prereq` — change the WARNING to a hard, classified fail-fast. **Do this one.**

`scripts/run-contract-tests.sh` already detects `git < 2.38` and already knows the tests "WILL
fail" — then proceeds anyway and produces a red run that is indistinguishable from a code defect.
That is precisely how `task_8454149df64b4d3f83905115708a4fed` lost correct work: 42 environment
failures were presented as a test result. The check should abort before pytest with a distinct,
machine-readable environment-prerequisite status, so the dispatcher classifies it as
`environment` at the source rather than by post-hoc inference from a truncated log. The version
gate belongs in the repository contract too: `toolchain.required_commands` lists `git` with no
version constraint, so nothing declares the 2.38 floor as a prerequisite.

### `host_image_git` — pin `git >= 2.38` in the fleet host image. **Do this one.**

Confirmed root cause. `2.34.1` is stock Ubuntu 22.04 / the GKE pod image; `merge-tree --write-tree`
landed in 2.38. This is not only a test problem — `src/mac/merge_queue.py` uses the same call in the
**production merge queue**, so any host running distro git cannot land a branch at all. Fix the
image; the gate change above is the guard rail, not the fix.

### `coverage_margin` — **do not chase the 0.03 pp. Close or repurpose it.**

There is no coverage regression to fix. On a `>=2.38` baseline the same tree measures
`82.15%` branches against an 80.00% floor — 2.15 pp of margin, not 0.03 pp of deficit. The
`79.97%` was an artifact of (a), twice over: the merge-gate tests' own 0.3468 pp, plus the
0.0239 pp of the serial phase their failure suppressed. Either contribution alone is close to or
larger than the 0.0260 pp the host was short. Raising coverage in response would be optimizing
against a broken measurement.

If the task is kept, repurpose it to fix the *reporting* defect that caused this misdiagnosis: when
`pytest_status != 0`, the coverage total is a partial measurement and the `coverage safety:` line
should say so (or be suppressed) rather than printing an authoritative-looking sub-floor percentage
that invites exactly the wrong conclusion. That is a one-line honesty fix with real diagnostic
value, and it is the only part of `coverage_margin` worth spending on.

## Out of scope, but found — flag for separate triage

Both failed run 1 on an unmodified baseline and are unrelated to git or to the parent task:

1. `tests/test_dispatch_route_contract.py::test_every_route_the_dispatch_layer_calls_exists` —
   `dispatch.py` calls nine hub routes that do not exist. Real contract drift, and it is
   **non-deterministic** (failed run 1, passed run 2), which makes it a latent flake on main.
2. `tests/cli/test_cli_task_update_dependencies.py::test_adding_a_dependency_does_not_re_evaluate_state` —
   the pinned known-limitation assertion no longer matches behaviour; the task now does transition
   to `waiting`. Either the service-layer policy decision the docstring defers was made, or this is
   a regression. The pin needs re-deciding, not deleting.

## Assumptions recorded

- **Different tree than the host measured.** This worktree totals 77464 statements / 25088 branches;
  the host evidence shows 82780 / 26936. The snapshot differs, so **absolute percentages are not
  comparable across the two** and none are compared here. Every conclusion rests on deltas measured
  within this tree between two otherwise-identical runs, then scaled onto the host's denominator.
  The causal argument survives a wide margin of error: it needs 7 branches and has 93.
- **12 cores, not 192.** The task brief anticipated a 192-core host; `nproc` reports 12. This
  affects wall-clock only (~16 min per whole-suite run), not outcomes.
- **The shim models one behaviour, precisely.** It reports `2.34.1` and rejects
  `merge-tree --write-tree` with the real 2.34 usage message and `rc=129`. It does not emulate every
  other 2.34-vs-2.39 difference, so 42 additional failures is a **lower bound** on what a genuine
  2.34.1 would produce. Since the finding is that (a) already over-explains (b) by 13x, a lower
  bound is sufficient.
- **AgentBus was unreachable from this sandbox.** `mac admin agentbus publish` is refused by egress
  policy (`POST host.openshell.internal:8789/agentbus not permitted by policy`), so the customary
  file-ownership announcement could not be sent. This task creates exactly one new file at the
  repository root and modifies nothing else, so the collision risk it would have advertised is nil.
