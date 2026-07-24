!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Close-Out: dream-finding review finalize/verify prerequisite

**Task**: Satisfy the parent acceptance criteria for the dream-finding scoped to
the *review finalize/verify* prerequisite — either apply the smallest
appropriate repair, or, if the finding is not actionable, produce a committed
close-out note recording the disposition.
**Upstream investigation**:
`docs/archive/field-notes/investigation-review-finalize-verify-prerequisite.md`.
**Prepared by**: fleet worker (remediation node; no production code edits).

## Determination: NOT ACTIONABLE — no source or test change applied

The upstream investigation established ground truth that the finalize/verify
prerequisite is **wired correctly and green on the current tree**, with no
reproducible defect. Under the parent acceptance criteria, when the finding is
not actionable the correct deliverable is this committed close-out note — not a
change to any reviewed module. No files under `src/mac/` or `tests/` were
modified for this finding.

## Why the finding is not a code defect

The investigation traced the full finalize/verify call chain and confirmed it
resolves as designed:

- `src/mac/review_finalizer.py:14` imports `run_deterministic_review_verdict`
  from `mac.task_executor` and `src/mac/review_finalizer.py:26` drives it with
  the workspace, review task, and `metadata.review_context`.
- `mac.task_executor` is a compatibility shim that aliases
  `mac.executor_sandbox` into `sys.modules[__name__]`
  (`src/mac/task_executor.py:1`); the canonical verdict function is defined once
  in `src/mac/executor_finalizer.py:1309`.
- The bridge fails closed: `src/mac/review_finalizer.py:29` raises
  `SystemExit("review finalizer did not produce mac-evidence.json")` when the
  manifest is missing, and `main()` returns `0` only when the manifest status is
  `complete`.

The real parent blocker is a host-owned checkout/sync invariant, not a code or
evidence defect. `run_deterministic_review_verdict` gates a repository review on
the exact executor commit being present at `HEAD` in the review checkout
(`git cat-file -e <exec_head>^{commit}` plus a `HEAD == exec_head` match) before
it runs bootstrap/tests/CodeGraph. Those invariants are satisfied by the
deterministic host finalizer's canonical fetch/rebase/checkout, which is outside
the worker boundary. A parent failure here reflects a review checkout that lacks
or is not sitting at the exact executor commit — an environment/sync condition —
rather than a defect in the reviewed sources.

## Corroboration in the task worktree

The finalize/verify contract suites cited by the investigation were re-run here
against the bootstrapped `.venv` and pass, reproducing the investigation's
result:

```
scripts/run-contract-tests.sh tests/test_review_finalizer.py \
  tests/test_dream_scanner.py tests/test_review_service_edges.py -q
# => all passed
```

`tests/test_review_finalizer.py` covers the complete/failed manifest status
mapping, the required review-task and `review_context` shape, and the exact
`did not produce mac-evidence.json` guard message — the full finalize/verify
contract the prerequisite depends on. There is no failing test, guard, or
reproducer to repair.

## Follow-up plan

1. **Close** the dream-finding review as *does-not-reproduce*; retain this note
   and the upstream investigation as provenance. No source or test change is
   warranted.
2. **Re-file at the correct layer if it recurs.** If a parent failure recurs on
   this line, open it against the host-owned checkout/sync path (canonical
   fetch/rebase/checkout of the exact executor commit), not the reviewed source
   modules.
3. **Reopen criteria.** Treat this finding as actionable against a reviewed
   module only if a failing assertion in the finalize/verify suites reproduces
   on the current tree, or a concrete reproducer localizes a defect to
   `review_finalizer.py`, `task_executor.py`, `executor_sandbox.py`, or
   `executor_finalizer.py`. Absent such evidence, the finding remains not
   actionable.

## Assumptions

- This is a `repo_change` task whose upstream determination is NOT ACTIONABLE,
  so the tracked deliverable is this close-out note rather than a code repair.
  Recorded here so the remediation outcome is auditable from repository history.
- Canonical synchronization, final tests/CodeGraph, commits of tracked
  modifications, and publication are owned by the deterministic host finalizer;
  this note is self-contained and unaffected by upstream drift.
