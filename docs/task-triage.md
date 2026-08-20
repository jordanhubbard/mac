# Triage: is this task still the right work?

A task is written at one moment and executed at another. In between, the work
may have landed by another route, been superseded, become unnecessary, or had
its scope invalidated. An agent that starts without checking redoes the
already-done thing — which is how one task produced five divergent
implementations and eleven duplicate pull requests, and how a merged module was
re-implemented after its task was reopened.

`mac.task_triage` answers the question once, cheaply, as part of claiming.

## Why the obvious version does not work

The natural signal is "does a merged pull request reference this task id".
Triaging 75 held tasks that way returned 24 hits, and **every one was a false
positive**. The id appears in changes that *cite* the task as evidence, not
that implement it: an ADR about the task-graph UI names a task because it
quotes it as motivating evidence, while the task is entirely outstanding.
Closing on that signal destroys live work.

Tightening to "task id in the title or branch" returns zero across the same 75,
correctly — a held task is never claimed, so no agent ever opens a change for
it. Both signals are wrong in opposite directions, which is why triage is a
judgement made by reading the repository rather than a query.

So the rule is structural rather than advisory:

> **A mention is not a citation.** A change whose only relation to the task is
> naming its id can never reach a closing verdict. `TriageDecision` refuses to
> be constructed that way, and `validate_close` refuses to let one out.

## The five verdicts

Following [ADR 0022](adr/0022-a-gate-returns-a-named-decision-not-a-boolean.md),
triage returns a named decision carrying its reason and its evidence — never a
boolean, never an `Optional` whose `None` means "work it out yourself".

| Verdict | Action | Meaning |
| --- | --- | --- |
| `already_landed` | close | The described change is present at head. Closes citing the commit that carries it. |
| `superseded` | close | A different change made this unnecessary. Closes naming the replacement. |
| `scope_stale` | update scope | Still wanted, but the described paths no longer match the tree. |
| `still_needed` | proceed | Nothing at head carries the work. |
| `cannot_tell` | proceed | Undecidable within budget. Proceeds, and the uncertainty is recorded. |

`cannot_tell` proceeds rather than blocking. A confident wrong verdict is worse
than an honest "unclear" — see the 24 false positives above.

Each verdict also carries a reason code from a closed set
(`landed_cited_change`, `mention_is_not_a_citation`, `scope_paths_missing`,
`partial_acceptance_evidence`, `budget_exhausted`, `no_head_evidence`,
`no_checkable_scope`, …). That code is what appears in the ledger and in
observability; there is no rendering layer that paraphrases it.

## What makes a task checkable

Triage reads two things from the task, and closes on neither alone:

- **`metadata.triage.scope_paths`** — repo-relative paths the work touches. If
  absent, backticked path-shaped tokens in the description are used, so a task
  that says it changes `src/mac/widget.py` has already said where to look.
- **`metadata.triage.acceptance_markers`** — strings whose presence at head
  means the described work is there. These are **only** taken from explicit
  metadata. Guessing a marker from prose was considered and rejected: a guessed
  marker that happens to match is exactly how a task gets closed on a
  coincidence.

`already_landed` requires **both** a change that touched the declared scope
**and** every declared marker present. A change that touched the file with the
markers absent is `still_needed`; markers only partially present is
`cannot_tell`, because "landed" and "someone is mid-way through it" look the
same from head.

A task with neither declared is `cannot_tell` — the honest answer, and the one
that would have been right 24 times out of 24.

## Bounded by construction

Triage is a read, and must not become a second execution. `TriageBudget` caps
git invocations, commits scanned, path probes, and wall-clock; `TriageCost`
records what the read actually spent and which limit stopped it. An exhausted
budget is `cannot_tell`, so the work proceeds. The cost is emitted as
`worker.triage.cost_ms` and travels with the verdict in the ledger.

## Where it runs

`MacWorker.execute_assignment` triages between repository preparation and the
coding agent — as part of claiming, not as a later sweep. Preparation has
already resolved the canonical branch head the task targets, so triage judges
that head rather than the task's own branch, at the cost of a handful of local
git queries and no network.

Routing:

- **close** — transitions the task to `cancelled` with the citation in the
  transition detail, and records it in the task narrative. The commit is named,
  not merely a change that mentions the task.
- **update scope** — `PUT /tasks/{id}` with the corrected `scope_paths`.
  [ADR 0020](adr/0020-a-running-task-is-not-editable.md) makes that atomic: the
  executor is aborted, the lease revoked, the change applied and the task
  restarted, so the next attempt re-derives everything from the corrected
  scope. Before that, correcting scope mid-flight would have created exactly
  the split brain ADR 0020 exists to remove.
- **proceed** — the assignment runs unchanged.

Every verdict is recorded on the task, including the ones that proceed: a
`cannot_tell` that proceeds silently is indistinguishable from no triage having
run. And a follow-up the hub declines — a worker token need not hold the
operator privilege to edit a task — is recorded and the work proceeds. A triage
that cannot apply its own conclusion is a reason to do the work, never a reason
to drop it on the floor.

## Using it outside the worker

The decision is pure and the fetch is separate, so both halves are usable on
their own:

```python
from mac.task_triage import collect_triage_evidence, decide_triage

evidence = collect_triage_evidence(task, worktree=checkout, head_sha=head)
decision = decide_triage(evidence)
if decision.closes:
    citation = decision.citation   # the commit, not a mention
```

`decide_triage` performs no I/O, so it can be tested adversarially against
hand-built evidence without a repository, a database, or a fleet.
