!!! warning "Historical field note"
    This record preserves prior investigation or planning evidence. It is not a current operating contract; use the numbered book and current runbooks for instructions.

# Investigation: dream-finding review finalize/verify prerequisite ground truth

**Task**: Establish ground truth for the dream-finding *review finalize/verify*
prerequisite so a low-confidence dream-cycle finding scoped to the review
finalizer subsystem can be assessed. Investigation only; no behavior changes.
**Parent task**: `task_a9f1989d82054ce3a56ec1d44755722d`
(contract-prerequisite for a dream-finding review).
**Repo areas mapped**: `src/mac/review_finalizer.py`,
`src/mac/task_executor.py`, `src/mac/executor_sandbox.py`,
`src/mac/executor_finalizer.py`, `tests/test_review_finalizer.py`.
**Investigated by**: fleet worker (investigation only; no production code edits).

## Status: PREREQUISITE IS INTACT — no reproducible defect

The review finalize/verify prerequisite is wired correctly and its contract
tests are green on the current tree. The finalizer bridge, its
`task_executor` alias hop, the canonical deterministic-verdict implementation,
and the manifest gate all resolve as designed. There is no reproducible defect
behind a finding that implicates this subsystem, so no source or test should be
changed on the strength of it.

## Ground Truth: The Finalize/Verify Call Chain Resolves

The CLI bridge imports the deterministic verdict entrypoint from the
compatibility module and drives it:

- `src/mac/review_finalizer.py:14` imports
  `run_deterministic_review_verdict` from `mac.task_executor`.
- `src/mac/review_finalizer.py:26` calls it with the workspace, the review
  task, and the review context extracted from `metadata.review_context`.

`mac.task_executor` is a compatibility shim, not a second implementation. It
aliases `mac.executor_sandbox` into `sys.modules[__name__]` so existing imports
and monkeypatches operate on the canonical module rather than a copy
(`src/mac/task_executor.py:1`). `executor_sandbox` in turn imports the verdict
function (`src/mac/executor_sandbox.py:247`) and calls it on the sandbox review
path (`src/mac/executor_sandbox.py:5031`).

The verdict function itself is defined once, in the finalizer module:
`run_deterministic_review_verdict` at
`src/mac/executor_finalizer.py:1309`. Its docstring names the deterministic
principle the prerequisite protects: the review agent owns the semantic
verdict; deterministic checks may *veto* an approval but must never convert a
semantic rejection into an approval.

A runtime import confirms the alias hop terminates at the canonical module:
importing `run_deterministic_review_verdict` from `mac.task_executor` resolves
to `__module__ == "mac.executor_finalizer"`.

## Ground Truth: The Finalize/Verify Prerequisite Is Enforced

The bridge does not trust the verdict blindly — it verifies the finalizer
actually produced a completion manifest:

- `src/mac/review_finalizer.py:27` re-reads `mac-evidence.json` from the
  workspace after the verdict runs.
- `src/mac/review_finalizer.py:29` raises `SystemExit("review finalizer did
  not produce mac-evidence.json")` when the manifest is missing (the verify
  prerequisite failing closed).
- `main()` returns `0` only when the manifest `status` is `complete`, and
  non-zero otherwise, so a non-complete verdict is a hard gate failure.

## Test Results (After Bootstrap)

Environment bootstrapped with `python3 scripts/bootstrap-project.py` (editable
install of `mac==0.1.0` succeeded; the pre-baked `.venv` already satisfied the
declared bootstrap outputs).

- Finalizer bridge suite via the canonical hermetic runner:
  `scripts/run-contract-tests.sh tests/test_review_finalizer.py -q`
  → `7 passed` (exit 0).
- Finalizer bridge plus prompt-finalizer edges:
  `scripts/run-contract-tests.sh tests/test_review_finalizer.py
  tests/test_executor_prompt_finalizer.py -q` → `11 passed` (exit 0).

`tests/test_review_finalizer.py:16` covers the complete/failed manifest status
mapping, the required review-task and `review_context` shape, and the exact
`did not produce mac-evidence.json` guard message — the full finalize/verify
contract the prerequisite depends on.

## Disposition

- **Actionable defect found**: none. The finalize/verify prerequisite is
  correctly wired and green.
- **Recommended follow-up**: close the dream-finding review as
  *does-not-reproduce*; retain this note as provenance. No source or test edit
  is warranted.

## Re-verification (2026-07-23): checkout-sync invariant is the parent blocker

A follow-up audit re-confirmed the finding above and pinpointed the exact
parent blocker as a checkout/sync invariant owned by the host finalizer, not a
code or evidence defect in the reviewed sources.

`run_deterministic_review_verdict` (`src/mac/executor_finalizer.py:1309`) gates
a *repository* review verdict on the exact executor commit before it will run
bootstrap and tests:

- It resolves `exec_head` from the executor evidence
  (`metadata.verification.repo.head_sha`) and treats the review as a repo
  review when that head is present.
- It runs `git cat-file -e <exec_head>^{commit}` in `MAC_TASK_REPO_WORKTREE`
  and reads `git rev-parse HEAD`. When the commit object is absent it sets the
  independent problem to `executor commit is not present in the review
  checkout`; when `HEAD != exec_head` it sets `review checkout HEAD does not
  match the executor commit`; either way the verdict is `rejected`.
- Only when both hold does it proceed to bootstrap, the contract test command,
  and the cooperative integration check.

Those two invariants are satisfied by the host finalizer's canonical
fetch/rebase/checkout, which is outside the worker boundary. So a parent
failure here reflects a review checkout that lacks (or is not sitting at) the
exact executor commit — an environment/sync condition — rather than a defect in
`review_finalizer.py`, `task_executor.py`, `dream_scanner.py`, or
`evidence_validators.py`.

Reviewed-source spot checks: `src/mac/review_finalizer.py` and
`src/mac/task_executor.py` are thin re-export/bridge shims with no verdict logic
of their own; `src/mac/dream_scanner.py` and `src/mac/evidence_validators.py`
contain the scan/validation helpers with no coupling to the checkout invariant.

Verification on the current tree:

- Named contract tests
  (`tests/test_review_finalizer.py tests/test_dream_scanner.py
  tests/test_review_service_edges.py`) → `43 passed`.
- Full canonical gate `scripts/run-contract-tests.sh` → `8667 passed,
  4 skipped` with coverage floors met (statements 90.87% ≥ 90.00%; branches
  80.25% ≥ 80.00%).

**Disposition unchanged**: no defect found in the reviewed sources; substantive
repair (if any) belongs to the dependent task and is a host-owned checkout-sync
condition, not a code change.
