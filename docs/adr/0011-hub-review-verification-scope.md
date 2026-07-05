# ADR 0011 - Hub review verification uses affected tests

- Status: **Accepted**
- Date: 2026-07-05
- Decision owner: MAC fleet owner

## Context

Repository tasks currently have two verification paths.

### Option A: task sandbox verifies before push

The worker prepares a task-owned repository worktree and the executor runs the
repository contract from `.mac/project.yaml`. For the MAC repository, that
contract's test command is `scripts/run-contract-tests.sh`, which runs the full
pytest/coverage gate.

The sandbox writes `mac-sandbox-verification.json` with the command, return code,
stdout/stderr, environment delta, and worktree path. During finalization,
`src/mac/worker.py` reads that file through `_sandbox_repository_verification_item`
and records it as the `repository contract test` with
`execution_environment=openshell_sandbox`.

If no sandbox verification file exists, the worker finalizer runs the contract
test itself from the task worktree. In both cases the pre-push gate uses
`_repository_finalizer_prepush_problems` to require:

- a clean repository snapshot with `dirty=false`;
- a valid `head_sha`;
- a non-empty `files_changed` list for normal repository work;
- at least one passing test/check;
- a passing CodeGraph audit for source/build/dependency/runtime changes.

Only after those local checks pass does the finalizer publish the task branch.
This means Option A is the publication gate for the task-owned branch.

### Option C: hub verifies the pushed branch during review

When `MAC_REVIEW_HUB_VERIFY` is enabled, the control plane can satisfy the
review verdict gate without dispatching a reviewer agent to set up its own
development environment. In `src/mac/services.py`, `_run_hub_review_verification`
looks at the executor evidence, confirms it describes a pushed repository change,
uses the selected reviewer's attestation key, and runs
`_hub_verify_run_contract_test`.

The hub verifier shallow-clones the pushed branch, archives the clone with
`.git` intact, uploads that archive into an OpenShell sandbox, runs a git
preflight, and then runs the repository contract command (currently the same
`scripts/run-contract-tests.sh` full suite for MAC). It records the result as a
signed `review_verdict` evidence manifest on the selected reviewer's behalf. A
zero exit code approves the review; a nonzero exit code rejects it with hub
contract-verification feedback.

Option C is therefore an independent review gate on the already-pushed branch,
but today it duplicates the same full-suite work that Option A already ran.

## Problem

The MAC contract suite is large: roughly 4,150+ tests when this decision was
raised, and recent runs collect more than 4,600 tests. Running the whole suite
with coverage takes multiple minutes in every per-task sandbox.

If Option C also runs the full suite for every review, each repository task pays
for the same expensive verification twice:

1. once in the task sandbox before the branch is accepted for publication; and
2. again in the hub OpenShell sandbox during review.

That does not scale with parallel fleet work. The hub review queue becomes a
second full-suite CI system, and the most common failure mode is no longer
"review found a bug"; it is "another long full-suite environment found a
flake, coverage-margin wobble, or infrastructure drift."

## Decision

Hub review verification should run **affected tests only**, not the full
repository contract suite, when the executor evidence already proves that Option
A ran the full contract suite successfully at the pushed commit.

The hub verifier remains independent: it must still clone the pushed branch and
run inside OpenShell. The change is the scope of the command it runs. Its job is
to confirm that the reviewed changeset is testable and that the tests most
directly coupled to the changed files still pass in the hub-controlled sandbox,
not to duplicate the publication gate.

The affected-test scope should be derived from the reviewed `files_changed`
set. For source/build/dependency/runtime changes, the selector should also use
the recorded CodeGraph affected-file audit. Directly changed test files are
included. If the selector cannot produce a reliable affected scope, the hub
verifier must fall back to the full repository contract command.

Fallback to the full suite is required when:

- executor evidence lacks a passing full-suite Option A result for the same
  `head_sha`;
- `files_changed` is missing or cannot be trusted;
- CodeGraph is required for the change and the executor evidence lacks a passing
  audit;
- the changes touch test selection, coverage configuration, repository contract
  configuration, bootstrap/runtime/dependency files, or broad shared
  infrastructure where affected tests cannot be bounded confidently;
- the affected selector returns no tests for a non-documentation code change.

For documentation/media/text-only changes, the hub affected-test command may be
a cheap deterministic check or no-op, provided the review verdict records why no
tests were selected and the executor evidence already contains the required
repository verification.

## Consequences

- Option A remains the canonical branch-publication gate and continues to run
  the repository contract's full suite.
- Option C remains a hub-controlled review gate, but it becomes proportional to
  the changed files instead of proportional to the whole repository.
- The review verdict manifest must record the selected test scope, the command
  that ran, and any fallback reason. It should continue to carry the executor's
  CodeGraph audit because the hub verifies the same commit.
- Full-suite coverage remains valuable, but it belongs at publication/mainline
  integration and at explicit fallback points, not as an unconditional duplicate
  in every hub review sandbox.
- The trade-off is that an affected-test review can miss unrelated integration
  regressions. That risk is accepted because the same pushed commit has already
  passed the full Option A contract, and the fallback rules preserve full-suite
  verification for broad or unmappable changes.

## Non-goals

This ADR does not implement the selector. It records the decision and the
required semantics for the follow-up implementation.
