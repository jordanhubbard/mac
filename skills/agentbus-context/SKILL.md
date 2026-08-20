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

**OBLIGATION `triage-against-branch-head`** — Triage a task against the
repository's top-of-branch before working it. Your worktree shows the repository
as it was when the worktree was cut; everything that landed since is on the bus
and nowhere else.

**OBLIGATION `mention-is-not-evidence`** — A change that merely MENTIONS a task
id never proves the work landed. `git.merged` naming your task, matched on its
`tree_sha`, is the evidence; a task id in a pull request title, a commit message
or a summary is not.

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
is durable and audited.

## Reach: which agents actually get this file

Honest scope, because a skill that overstates its own delivery is worse than
none:

* An agent working **in the mac repository** reads this because `AGENTS.md`
  and `CLAUDE.md` at the repo root point at `skills/`, and every coding CLI
  reads those.
* An agent working in **any other mac-managed project** does not get this file
  automatically. What reaches it regardless is the executor policy
  (`src/mac/executor-policy.txt`, delivered into every task sandbox as
  `.mac-executor-policy.txt` and named as the top authority in every task
  prompt) plus the **AgentBus context** section the worker attaches to the
  task. Those are prompt-level and project-independent; this file is not.
* Beyond that, `mac admin skills install` renders `skills/` into a harness
  MAC does not own — `global` for the user's own configuration, or a
  repository they nominate explicitly. It is an operator action rather than
  something MAC does on its own, because writing into a working tree nobody
  asked about is how a plugin gets uninstalled. The obligations above are
  delivered into the harness's always-on surface; the rest of this file is
  delivered as an on-demand reference.
