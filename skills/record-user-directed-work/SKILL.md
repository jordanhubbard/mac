---
name: record-user-directed-work
description: Turn user direction, corrections, and discovered defects into durable ledger tasks before implementing them. Use whenever you change this repository or the fleet in response to a conversation, or find follow-up work while executing.
---

# Record user-directed work

Convert conversation into durable state before changing anything. A plausible
reading of a request is not permission to improvise, and a finding that lives
only in a chat reply is a finding that is lost.

For mac the durable store is the **task ledger** (`mac task`), not a roadmap
file. `.tickets/` is a gitignored local mirror and is never the record.
Cross-session knowledge that is not a unit of work — a fact worth carrying
forward rather than something to do — goes to the memory store instead:

    mac admin memory remember <key> <content> --project=mac

Note the `admin`: the memory verbs are no longer top-level `mac memory`. Both
stores live under the hub; neither lives in a markdown file in the tree.

## Record before implementing

1. Separate the outcome the user wants from the implementation they proposed.
   Say which you are acting on.
2. File it:

       mac task create "title" --description-file=f.txt --as-human <user>

   Use `--description-file` for anything with parentheses, backticks, `$VAR` or
   newlines — shell quoting will otherwise mangle it.
3. The description carries the reasoning, not just the request: what was
   observed, what it implies, the scope boundary, and what evidence would show
   it fixed. A title alone is a reminder, not a record.
4. Prefer updating an existing task when the direction refines it. A second
   task about the same defect splits its evidence. Search before filing:

       mac task search <keyword>

Diagnosis may precede recording — you often cannot describe the work until you
have looked. Do not change source, policy, or fleet state first.

**Emergency containment is the one exception.** If the fleet is degraded and a
reversible action restores it, act, then record in the same turn and say why
the order was inverted.

## Editing a task that is already in flight

`mac task update` is safe on a RUNNING or CLAIMED task, but not because it
edits in place — it does not (ADR 0020). Underneath, the hub aborts the
executor, revokes the lease, applies the change and restarts the task, as one
operation. Ask for the update and you get an update; there is no half-done
state to clean up and no window in which the ledger and the executor disagree.

Three consequences that change what you should do:

- **A restart is not a resume.** The next agent re-enters the task from the
  top and re-derives everything from the task as it now stands. Do not write an
  updated description that assumes the reader saw the old one.
- **Evidence from the aborted attempt is history, not current state.** It stays
  attached and queryable, but nothing may treat it as satisfying a requirement
  of the restarted attempt.
- **Restarts are not attempts.** Correcting a task's scope does not consume its
  retry budget, and it does not reset it either.

`stop` and `start` also exist standalone, for halting runaway work or freeing
an agent without changing anything:

    mac task stop <id> --reason "..."
    mac task start <id>

STOPPED is a real state, and it is **not** terminal: it is excluded from every
sweeper, reaper and dispatch pass, burns no attempt budget, and waits
indefinitely. That is also how a stopped task gets forgotten — nothing will
nudge you. If you stop something, say in the same turn what will start it.

`start` returns the task to OPEN, or to WAITING if the edit left a dependency
unmet, evaluated at that moment rather than assumed.

Under the ACL model (ADR 0019) `update`, `stop` and `start` are separate
permissions, split out of `write` and `control`, and no permission implies
another. An agent's task-scoped credential carries `read`, `append` and
`create` — so an agent can neither stop its own task to escape a gate nor
rewrite the criteria it is being judged against. If you need one of those,
ask; do not look for a way around it.

## While executing

- File newly discovered defects as their own tasks **at the moment you find
  them**. Do not carry them to the end of the turn: a defect described only in
  a final summary is one the reader has to re-derive from prose, and it will be
  lost the moment the session ends. If you catch yourself writing "worth a
  ticket", file it instead of writing that.
- Record what you tried and rejected when it is not obvious, especially a
  hypothesis that testing disproved. The next reader will otherwise repeat it.
- Never let chat memory be the only place a decision exists.

## Filing work that must not dispatch yet

Filing with the hold on keeps a task real without letting it be claimed:

    mac task create "title" --description-file=f.txt --no-dispatch

Use it when the work is wanted but its prerequisite has not landed — staging a
backlog behind a fix, rather than releasing it against a fleet that still
carries the defect.

The hold is metadata, and clearing it is its own verb:

    mac task release <id>

Nothing clears it for you, so a `--no-dispatch` task with no one watching is
indistinguishable from a forgotten one. If a task is not being claimed and you
do not know why, do not guess:

    mac task why-unclaimed <id>

It names the closed gate — `task_dispatch_held`, `task_project_paused`,
`task_dependencies_unmet`, `agent_held`, `agent_project_not_allowed`, and the
rest — instead of leaving you to infer it from an idle queue.

## Fleet-affecting work has an ordering rule

Deploy the fix before releasing work that depends on it.

The order is: fix -> merge -> deploy -> verify one canary completes -> release
the backlog. Skipping the verification step is how a fleet gets a second
outage from the repair.

The reason is the retry classifier. It reads the failure's **message**, not its
reason code: a blob containing `tests failed`, `verification_contract_failed`,
`repo evidence requires` or `required changed files` is classified
`non_retryable` and the task stops there. So work released into a fleet that
still carries the defect does not simply fail and come back — a whole tranche
of it can land in FAILED on its first attempt.

Two things follow that are easy to get backwards:

- **An unrecognised failure is `transient`, not `non_retryable`.** That is
  deliberate: classifying the unknown as permanent meant an infrastructure
  failure where the model never ran went terminal at attempt 1 of 3. Do not
  reason from the reason code; a `repository_test_failed` whose detail says
  only "exited with status 1" is retried.
- **The attempt counter increments at claim time**, so an attempt is consumed
  whether or not the work ever started.

Recovering a task that exhausted its budget:

    mac task reopen <id> --reason "..."

That resets `attempt_count` and clears `completed_at`, so the requeue is not
immediately re-exhausted. Read the per-attempt failures in its history for the
recurring cause first; reopening without fixing that just spends the budget
again.

## Closing

1. Run the evidence the task named, at the scope it claimed.
2. Close it with the durable result — a test target, a commit, an observed
   state — not "done":

       mac task close <id> --reason="..."

3. Leave partial work open across commits and sessions. An unchecked item is
   information; a checked one that was not verified is a lie the next agent
   will act on.
