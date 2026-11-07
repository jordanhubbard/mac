---
name: problem-response
description: On any problem: fix it, do RCA, mitigate root cause. Use whenever you encounter a bug, data rot, repeated mistake, or process failure. Never stop at the fix.
---

# Fix → RCA → Mitigate

**Applies to: all fleet agents.**

Whenever you encounter a problem — a bug, data rot, process failure, repeated
mistake — do all three steps in order. Never stop at the fix.

## Step 1: Fix the problem

Resolve the immediate symptom. Clean up the damage. Leave the system in a
correct state before moving on.

## Step 2: Root cause analysis

Ask: what actually caused this? Not the symptom — the underlying condition that
made the symptom possible.

Useful questions:

- What assumption was wrong?
- What check was missing?
- What process allowed this to repeat?
- Would this have happened if the system were designed differently?

State the root cause explicitly in one sentence before continuing.

## Step 3: Root cause mitigation

Change something so the root cause cannot produce the same symptom again.

Mitigation forms (pick the strongest available):

- **Code/script change**: add the missing check, guard, or dedup logic directly
- **Skill** (this repository): write or update a skill in `skills/` and push;
  it deploys fleet-wide at next context refresh
- **MAC task**: file a task if the mitigation requires non-trivial work
- **Axiom / memory**: if correct behavior must survive sessions, pin it in the
  agent's memory graph

A fix without mitigation is a patch. A patch without RCA is a guess. All three
together is the standard.

## Examples

### Duplicate memory nodes (2026-11-06)

**Fix**: deleted 6 duplicate nodes from the graph.

**Root cause**: striving cron called `add_node()` on every run with no prior
search — the same philosophical thought was re-added 7 times across 7 runs.

**Mitigation**: created `memory-add` Hermes skill (search-before-add protocol),
deployed to all agents; cron script queued for update.

### AgentFS paths in memory_graph README (2026-11-05)

**Fix**: replaced deprecated `~/AgentFS/` paths with canonical
`~/.mac/data/` paths.

**Root cause**: README was written before the AgentFS deprecation and was
never audited after the migration.

**Mitigation**: filed follow-up task to audit all doc files for AgentFS
references; added this skill so agents have a durable protocol for future
similar finds.

## Pitfalls

- Don't conflate symptom with root cause. "Node was duplicated" is the symptom;
  "no dedup check before `add_node()`" is the root cause.
- Mitigation must change something structural — "be more careful" is not a
  mitigation.
- If the mitigation requires a task, file it in MAC before closing the
  conversation.
- Skills in `skills/` reach all fleet agents when merged to main; prefer them
  over per-agent Hermes memory for protocol changes that all agents must follow.
