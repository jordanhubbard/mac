# ADR 0031: CodeGraph is a hint when the tool and `.codegraph/` exist, not a hard gate

- Status: Proposed
- Date: 2026-08-23
- Decision owner: MAC fleet owner
- Related: [ADR 0011](0011-hub-review-verification-scope.md) — hub-verify uses
  the sanity contract; this ADR changes what that contract does when CodeGraph
  cannot be trusted
- Related: [ADR 0022](0022-a-gate-returns-a-named-decision-not-a-boolean.md) —
  "CodeGraph failed" is not a decision; the named outcomes below are
- Numbering: **0029** is claimed by the in-flight route-ladder ADR on #634

## Context

CodeGraph is a legitimate analysis tool. It is also known to lie on complex
trees: incomplete indexes, lock stalls, `affected` exiting non-zero, and
JSON that names the wrong tests. Treating those failures as proof that the
change is unsafe is how a passing, pushed branch becomes a review reject.

Observed 2026-08-23 on PR #634 (`task_ae2fc223`):

1. The first hub-verify reject was real — a docs example used an operator
   identity. That belongs to `test_docs_no_operator_identity`. It was fixed.
2. The second reject selected `sanity selection: full (codegraph_affected_failed)`
   and ran the entire contract suite. Eleven failures, about eleven minutes,
   none of them the defect under review.

The flip lives in `scripts/resolve-impacted-tests.py`. CodeGraph is unioned
into the focused set, then:

```
if unresolved_source and (codegraph_problem is not None or not codegraph_tests):
    return _full(codegraph_problem or "unresolved_source_without_reliable_affected_tests", ...)
```

`codegraph affected` returning non-zero becomes `codegraph_affected_failed`,
which becomes a full run. A missing binary becomes `codegraph_unavailable`
and the same full run. The selector cannot tell "the index lied" from "we
do not know the blast radius".

The worker pre-push gate is the same class. `mac.codegraph_audit.v1` is
required for any source/build/dependency/runtime change. `run_codegraph_audit`
**fails** when the binary is missing (`codegraph_not_available`) and when
`init`/`sync`/`affected` is non-zero. `codegraph_audit_manifest_problems`
then blocks publication unless status is exactly `pass` and both an index
command and an `affected` command succeeded. AGENTS.md states the same
rule as a hard evidence gate. A docs-only change may record
`codegraph.status=skipped` with `reason=non_code_change`; a Python change
may not.

So a tree without a trustworthy `.codegraph/`, or a tool that cannot answer,
cannot publish — or, if it gets past the worker, pays for the full suite
at review. That is the wrong place for this check.

## Decision

### 1. CodeGraph is a hint, never a gate

A hint may widen a focused test set. It may not:

- fail the worker pre-push audit by itself
- fail hub-verify by itself
- flip sanity from focused (or from the repository's declared test command)
  to a full-suite run

The declared test command and the changed-file list remain the gate.
CodeGraph, when it answers, is extra names in that list.

### 2. A hint is only eligible when both prerequisites exist

CodeGraph may be consulted if and only if:

1. a `codegraph` binary is on `PATH` (or `MAC_CODEGRAPH_BIN`), **and**
2. a `.codegraph/` directory already exists for that source tree.

If either is missing, the decision is `hint_unavailable`. Do not run
`codegraph init` to create an index just so the gate has something to
fail closed on. An index created under time pressure in a sandbox is
exactly the index that lies.

If both exist and `affected` (or `init`/`sync`) fails, times out, or
returns unusable JSON, the decision is `hint_untrusted`. Same
consequence as unavailable: ignore the hint.

If both exist and `affected` returns a usable test list, the decision
is `hint_applied`: union those tests into the focused set.

### 3. The decision is named (ADR 0022)

| Code | Meaning | What the gate does |
| --- | --- | --- |
| `hint_applied` | tool + `.codegraph/` present; `affected` usable | union the extra tests; continue |
| `hint_unavailable` | no binary, or no `.codegraph/` | skip CodeGraph; use changed files + declared tests |
| `hint_untrusted` | tool and index exist; output failed or is not believed | skip CodeGraph; do **not** escalate to full |
| `non_code_change` | no source/build files in the diff | skip, as today |

`codegraph_affected_failed` and `codegraph_not_available` stop being
reasons for `_full(...)`. They map onto `hint_untrusted` and
`hint_unavailable`. Unresolved source without a hint is still allowed
to fail closed *on the impact map* (`unresolved_source_without_reliable_affected_tests`)
only when the repository has no other declared way to test the change.
It is not allowed to blame CodeGraph for that escalation.

### 4. `mac.codegraph_audit.v1` records the hint; it does not block

The worker still runs the audit when the hint is eligible, and still
records the manifest. Status values that must be legal on a *passing*
publication:

- `pass` / `hint_applied`
- `skipped` / `non_code_change`
- `skipped` / `hint_unavailable`
- `skipped` / `hint_untrusted`

`codegraph_audit_manifest_problems` returns empty for those. It does
not demand a successful `init`/`sync`/`affected` pair as a condition of
push. `codegraph.status=skipped` is no longer reserved for docs-only
changes.

AGENTS.md, the executor policy, and the pre-push evidence list change
to match: CodeGraph is analysis support, recorded when used, never a
hard requirement.

## Consequences

- A branch whose own tests pass publishes even when CodeGraph is absent,
  stale, or wrong. That is the #634 second-reject fix.
- Focused sanity may be slightly smaller on complex trees (the lying
  `affected` list is dropped rather than replaced by the entire suite).
  The cost of a missed edge is paid at mainline integration (ADR 0011),
  not by every review.
- Deploy can keep installing CodeGraph. Installation is not a gate.
  Presence of the binary without a `.codegraph/` directory is
  `hint_unavailable`, not a fail.
- Tests the task named become the pin:
  - `codegraph_affected_failed` does not flip sanity to full-suite
  - a repository without `.codegraph/` still publishes when its own
    tests pass
- Call sites that must change: `scripts/resolve-impacted-tests.py`
  (`codegraph_affected`, the `_full` branch), `src/mac/codegraph_audit.py`
  (`run_codegraph_audit`, `codegraph_audit_manifest_problems`),
  `src/mac/worker.py` / `executor_finalizer.py` / `evidence_validators.py`
  (publication problems), AGENTS.md and the executor policy.

## Alternatives considered

**Keep fail-closed; fix CodeGraph so it stops lying.** Rejected as the
gate. Improving the index is useful analysis work. It does not justify
blocking publish or expanding to the full suite when the tool is wrong
*today*. A gate that depends on an untrusted oracle will keep producing
#634's second reject.

**Treat only a missing binary as skippable; keep `affected` failure as
full-suite.** Rejected. That is the current behaviour for any tree that
has the tool installed, which is every fleet image. The lie is in
`affected`, not in `which codegraph`.

**Drop CodeGraph entirely.** Rejected. When the index is present and
`affected` answers, the extra tests are cheap and sometimes right. A
hint that sometimes helps is worth recording. A requirement that
sometimes lies is not.

## Non-goals

- Replacing the impact map or the declared repository test command.
- Changing what `codegraph init` / `codegraph affected` compute when an
  operator runs them by hand.
- Making CodeGraph optional to *install* on fleet images. Install it.
  Do not require its answer.
