---
name: judgement
description: Hub-owned checklist for judging task-lifecycle gate quality and intervening on process defects. Read by the hourly judgement process and by any operator inspecting why the hub stopped a task, held an agent, or redeployed the fleet.
---

# Judgement

This skill is the hub's process-quality checklist. The hourly judgement
process (`src/mac/judgement.py`) reads it and acts. It does not run in a
sandbox. It has the current mac checkout and the same privileged verbs an
operator has: stop a task, hold an agent, stop the fleet, redeploy an image,
and start the appropriate entities back up.

It judges the **quality** of the gates on the claim/fix/deliver cycle, the
**number** of those gates, and the **states** tasks have wound up in. It does
not re-review the content of a task. Hub-verify (deterministic contract tests)
is the only default review gate. There is no LLM semantic reviewer.

## Checklist

Each item is a finding kind the process emits. If the evidence matches, it
intervenes.

### 1. `review_rejection_loop`

Work already passed its tests and was pushed, then a reviewer rejected it,
then the next attempt did the same thing.

Observed 2026-08-23 on `task_3b44296d` (docs audit): four passing, pushed
attempts; a fleet reviewer and the hub-verify reviewer rejected each one;
17 million tokens; no publication. Same shape on `task_ae2fc223`
(route-ladder ADR): two hub-verify rejections after a passing push, then a
contract-gate failure that blocked two other P0 release tasks.

**Intervene:** stop the looping task. If the assignee is still burning
tokens on the same evidence, hold that agent. Do not assign another
semantic reviewer — that gate is gone.

### 2. `high_token_without_publication`

A task has spent a quarter-million tokens or more and produced no
publication. The ledger already raises `high_token_work_without_publication`.

**Intervene:** stop the task. If the owner is still in-flight on it, hold
the owner until an operator or a later judgement cycle releases them.

### 3. `failed_dependency_deadlock`

A task is blocked on a dependency that is `failed` (or otherwise terminal
and unsuccessful) under `all_success`. The child can never dispatch. Two P0
release blockers (`task_529caba5`, `task_6086c172`) sat in this state behind
`task_ae2fc223`.

**Intervene:** stop the blocked child so it stops looking live. Do not
reopen the failed parent automatically — that is how a broken ADR gets
re-implemented. Record the pair and leave the parent for an operator or a
code-level process fix.

### 4. `stuck_reviewing`

A task has been in `needs_review` or `reviewing` longer than the stuck
threshold (default two hours) without a publication. Twenty-two tasks were
in `reviewing` on 2026-08-23, including a P0 titled "Diagnose why the hub
self-tick fails to drain the 66 REVIEWING tasks".

**Intervene:** if hub-verify can still run, leave it for the next review
sweep. If it is waiting on an LLM reviewer, stop the task. If a
non-virtual agent holds the review, hold that agent.

### 5. `semantic_reviewer_still_assigned`

A pending review is assigned to a fleet agent (not the virtual
hub-reviewer). That is the gate this skill exists to keep gone.

**Intervene:** stop the task under review. If many such assignments are
live, stop the fleet, redeploy the current mac image, and start only the
hub-reviewer plus the workers that were not part of the defect.

### 6. `excessive_reviewing_population`

More than a tenth of non-terminal tasks, or more than twenty tasks, sit in
`needs_review` / `reviewing`. That is a process pile-up, not a busy day.

**Intervene:** stop the oldest stuck reviews first. If the pile-up persists
across a cycle, hold the worker fleet so new work stops feeding the queue,
then redeploy.

### 7. `too_many_gates`

A single attempt collected three or more distinct review or contract
rejections after already presenting passing executor evidence. The
claim/fix/deliver cycle has grown a gate that does not earn its keep.

**Intervene:** stop the task. Treat the extra gate as a process defect —
the same class of defect that made the semantic reviewer worth removing.

### 8. `orphaned_pull_request`

An open PR names a task that is already `completed` or `cancelled`, or
the same task id already has a merged PR. Observed 2026-08-23: 56 open
PRs against `main`, zero review decisions. Several were copies of work
that later landed under another number (`#585` after `#577`, `#587`
after `#580`, `#582`/`#612` after `#614`).

**Intervene:** close the orphaned PR. Do not open a replacement. The
branch is archaeology, not a second review queue.

### 9. `duplicate_pull_request`

Two or more open PRs name the same task. The deploy-generation
retirement record was opened five times (`#485`, `#609`–`#613`). Task
stop/restart was opened three times (`#514`, `#641`, `#642`).

**Intervene:** close every older duplicate. Keep the newest. Do not
ask another agent to re-implement the same change.

### 10. `unlanded_pull_request`

A PR is still open, the task is `failed` / `blocked` / `reviewing`,
and nothing with that task id has merged. This is the good work that
got stuck in semantic review and never landed — `#643` (docs audit),
`#634` (route-ladder ADR), and dozens more.

**Intervene:** stop the looping task. Do **not** close the PR. The
branch is the salvage. Hub-verify is the only gate left; a later
operator or judgement cycle can land it. Closing it is how the work
disappears a second time.

## Authority

The process may:

- `mac task stop` any live task
- `mac agent hold` / `mac agent resume` any non-virtual agent
- pause every registered project (fleet stop) and activate them again
- invoke the fleet redeploy command against the current mac checkout
- start stopped tasks and resume agents it held, after a redeploy

It may not invent a new review gate. It may not restore the semantic
reviewer. Every action is audited as `judgement.*` observability with the
finding kind that licensed it.

## Bounds

A runaway judge is worse than a silent one.

- At most `MAC_JUDGEMENT_MAX_ACTIONS_PER_CYCLE` interventions per hour
  (default 20).
- At most `MAC_JUDGEMENT_MAX_REDEPLOYS_PER_DAY` redeploys (default 2).
- Holds it places are tagged `judgement:` so a later cycle can resume
  only what it held, not an operator hold.
- Redeploy is injected in tests and fail-closed if the command is missing.
