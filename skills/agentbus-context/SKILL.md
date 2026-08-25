---
name: agentbus-context
description: Read the fleet's broadcast bus BEFORE you start a task — has this work already landed, has the trunk moved under you, is a peer holding the file you are about to edit. Consulting the bus is step zero of starting work, not an optional extra.
---

# Read your messages before you dive in

You are one of several agents working the same repositories at the same time.
Everything you can learn from the repository is what it looked like when your
worktree was cut. Everything that happened *since* — a peer's branch, a merge
onto the trunk, your own task's change landing while you were queued — is on
the AgentBus broadcast channel and nowhere else.

**This has already cost real work.** Eight pull requests (#405, #437, #442,
#443, #445-448) were opened against changes that were already merged, because
no agent could find out that its own task's work had landed. The information
existed; nothing read it.

## Step zero of every task

Before your first edit, answer three questions:

1. **Has this task's work already been published?** If `git.merged` names your
   task, the change is in the trunk. Verify against the repository, say so in
   the evidence, and stop — do not redo it and do not open a second pull
   request.
2. **Has the canonical tip moved under me?** If `git.canonical_advanced` names
   the branch you were cut from, rebase before you push instead of finding out
   at push time.
3. **Is a peer holding something related?** A `git.worktree_added` or
   `task.claimed` from another agent in this repository means you are not
   alone in it. If a peer says they own a file you were about to change,
   believe them.

When MAC runs you as a fleet worker, the answers are already in your prompt: a
section headed **"AgentBus context"**, gathered by the worker before your task
started. Read it first. It carries at most 50 events and says out loud when it
clipped any — if it says `TRUNCATED`, the context is incomplete and you must
query the hub rather than assume you saw everything.

When you are *not* running under a worker (a human-launched session, a repo
checked out by hand), ask the hub yourself:

    mac admin agentbus broadcast $MAC_AGENT_ID --limit 50
    mac admin agentbus broadcast $MAC_AGENT_ID --event-types git.merged,git.canonical_advanced
    mac admin agentbus broadcast $MAC_AGENT_ID --project mac --limit 20

## The vocabulary, and what each verb licenses you to do

| event | it means | what you do about it |
| --- | --- | --- |
| `git.branch_created`, `git.worktree_added` | a peer has a live checkout on that branch | do not stage in that checkout; do not assume the branch is yours |
| `git.pushed` | a branch reached the shared remote | fetch before you compare against it |
| `git.pr_opened` | a pull request exists for that task | do not open another one for the same task |
| `git.merged` | that task's work is IN the trunk | if it is your task: stop, verify, report — never re-do or re-PR |
| `git.canonical_advanced` | the trunk moved | rebase onto the new tip before pushing |
| `git.merge_conflict` | a peer hit a conflict against the tip | expect the same conflict; coordinate |
| `sandbox.policy_changed` / `sandbox.policy_published` | the guardrail moved | the worker holds new work itself; you do not need to act mid-task |

## Traps

**`git.merged` carries a `tree_sha`, and that is the field you match on.**
Every merge in this fleet is a *squash*: the commit sha in the event was minted
at merge time and matches nothing you ever held. Tree identity survives the
squash — it is what `native_merge_queue.landing_is_safe` gates on, and it is
why the terminal events carry it.

**Your own echo is not news.** Events you emitted come back with
`self_emitted: true`. The worker filters them out before you see them; if you
read the feed yourself, filter them yourself.

**Announce what you will touch; do not narrate.** A peer can act on "I am
editing src/mac/api.py". Nobody can act on a status update, and every message
is durable and audited. Heartbeat is liveness, not progress: a session that
only heartbeats still looks stuck.

**Broadcast the work; do not nudge a silent peer.** Claim, worktree, push, PR,
and merge are how others learn you are in the tree. If you are stuck, address
the hub. Do not broadcast a plea, and do not inbox a peer who has gone quiet —
only the hub tick may send a stall nudge (ADR 0023).

## Reach: which agents actually get this file

Honest scope, because a skill that overstates its own delivery is worse than
none:

* An agent working **in the mac repository** reads this because `AGENTS.md`
  and `CLAUDE.md` at the repo root point at `skills/`, and every coding CLI
  reads those.
* After `mac admin plugin install`, the same skills are on the harness
  (Claude/Cursor/Codex/OpenCode/Pi pointers at one `$MAC_HOME/plugin` copy).
  That is how a session outside this repository gets the obligation.
* Until that installer has been run on a host, an agent working in **any
  other mac-managed project** does *not* get this file from the repo.
  What reaches it instead is the executor policy
  (`src/mac/executor-policy.txt`, delivered into every task sandbox as
  `.mac-executor-policy.txt` and named as the top authority in every task
  prompt) plus the **AgentBus context** section the worker attaches to the
  task. Those are prompt-level and project-independent; this file is not.
