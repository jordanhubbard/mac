# ADR 0011 - Hub review verification uses affected tests

- Status: **Accepted**
- Date: 2026-07-05
- Amended: **2026-07-06** by `docs/testing-strategy.md`
- Amended: **2026-07-25** to make current-main publication the central full gate
- Decision owner: MAC fleet owner

## Context

Repository tasks currently have two verification paths.

### Option A: task sandbox verifies before push

The worker prepares a task-owned repository worktree and the executor runs the
repository contract from `.mac/project.yaml`. For the MAC repository, that
explicit sanity contract is `scripts/run-sanity-tests.sh`. It selects changed,
CodeGraph-affected, public-contract, and process-E2E tests and falls back to
`scripts/run-contract-tests.sh` whenever the scope is broad or uncertain.

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
This means Option A is the proportional publication gate for the task-owned
branch. Full statement/branch/subprocess coverage is the mainline integration
gate rather than a per-task repetition.

### Option C: hub verifies the pushed branch during review

When `MAC_REVIEW_HUB_VERIFY` is enabled, the control plane can satisfy the
review verdict gate without dispatching a reviewer agent to set up its own
development environment. In `src/mac/services.py`, `_run_hub_review_verification`
looks at the executor evidence, confirms it describes a pushed repository change,
uses the selected reviewer's attestation key, and runs
`_hub_verify_run_contract_test`.

The hub verifier shallow-clones the pushed branch, archives the clone with
`.git` intact, uploads that archive into an OpenShell sandbox, runs a git
preflight, and then runs the repository sanity contract with evidence-derived
`--changed-file` arguments. Repositories or older branches without that explicit
contract fall back to `scripts/run-contract-tests.sh`. It records the result as a
signed `review_verdict` evidence manifest on the selected reviewer's behalf.

A zero exit code approves the review. A nonzero exit code is split by
`mac.review_verdict.classify_contract_run` before it is signed: a run whose
suite never collected (collection errors, a broken conftest, a sandbox
transport fault) is recorded as `infrastructure`, and anything else is recorded
as `tests_failed`. The hub never reads the diff, so it has no semantic axis to
offer and must not sign `rejected` — that word means a reviewer judged the work.
An `infrastructure` verdict reopens the task without spending an attempt; see
`docs/review-verdict-axes.md`.

Option C is therefore an independent, proportional review gate on the
already-pushed branch.

## Problem

The MAC contract suite is large: more than 5,000 cases, with full branch and
subprocess coverage taking multiple minutes. Running the whole suite in every
per-task sandbox is not proportional to the change.

If Option C also runs the full suite for every review, each repository task pays
for the same expensive verification twice:

1. once in the task sandbox before the branch is accepted for publication; and
2. again in the hub OpenShell sandbox during review.

That does not scale with parallel fleet work. The hub review queue becomes a
second full-suite CI system, and the most common failure mode is no longer
"review found a bug"; it is "another long full-suite environment found a
flake, coverage-margin wobble, or infrastructure drift."

## Decision

The task-owned branch and hub review verification should run the explicit
**affected + canary sanity contract**, not the full repository contract suite,
when selection is trustworthy. Neither phase should duplicate an identical
test scope already proven at the same commit.

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

Canonical publication is a different boundary: immediately before pushing
main, the merge queue computes the exact merge of the reviewed commit onto the
current main tip, materializes that projected tree in a disposable checkout,
and runs the full configured repository contract in the existing OpenShell
verification boundary. It then requires the actual local merge tree to equal
the tested projection before push. This is one full-suite run for the serial
integration point, not one full run per branch and another per review.

## Consequences

- Option A remains the task-branch gate and runs the explicit sanity contract,
  which fails closed to the full suite for broad or uncertain changes.
- Option C remains a hub-controlled review gate, but it becomes proportional to
  the changed files instead of proportional to the whole repository.
- Canonical current-main publication is the central full-contract gate. A
  conflict, missing command, test failure, verifier error, or tested-tree
  mismatch blocks the push.
- The review verdict manifest must record the selected test scope, the command
  that ran, and any fallback reason. It should continue to carry the executor's
  CodeGraph audit because the hub verifies the same commit.
- Full-suite coverage remains valuable at mainline integration, scheduled
  audits, and explicit fallback points, not as an unconditional duplicate in
  every task and review sandbox.
- The trade-off is that an affected-test review can miss unrelated integration
  regressions. Public/process canaries limit that risk, broad or unmappable
  changes fall back to the full suite, and mainline runs the complete coverage
  gate before a release is accepted.

## Non-goals

This ADR does not prescribe a particular CodeGraph implementation. The selector
and its fail-closed rules are implemented by `scripts/select-sanity-tests.py`
and `scripts/run-sanity-tests.sh`.
