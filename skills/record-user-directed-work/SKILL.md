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

## Record before implementing

1. Separate the outcome the user wants from the implementation they proposed.
   Say which you are acting on.
2. File it: `mac task create "title" --description-file=f.txt --as-human <user>`.
   Use `--description-file` for anything with parentheses, backticks, `$VAR` or
   newlines — shell quoting will otherwise mangle it.
3. The description carries the reasoning, not just the request: what was
   observed, what it implies, the scope boundary, and what evidence would show
   it fixed. A title alone is a reminder, not a record.
4. Prefer updating an existing task when the direction refines it. A second
   task about the same defect splits its evidence.

Diagnosis may precede recording — you often cannot describe the work until you
have looked. Do not change source, policy, or fleet state first.

**Emergency containment is the one exception.** If the fleet is degraded and a
reversible action restores it, act, then record in the same turn and say why
the order was inverted.

## While executing

- File newly discovered defects as their own tasks **at the moment you find
  them**. Do not carry them to the end of the turn: a defect described only in
  a final summary is one the reader has to re-derive from prose, and it will be
  lost the moment the session ends. If you catch yourself writing "worth a
  ticket", file it instead of writing that.
- Record what you tried and rejected when it is not obvious, especially a
  hypothesis that testing disproved. The next reader will otherwise repeat it.
- Never let chat memory be the only place a decision exists.

## Fleet-affecting work has an ordering rule

Deploy the fix before releasing work that depends on it. Reopening tasks into
a fleet that still carries the defect converts recoverable tasks into
permanently failed ones — `repository_test_failed` and its relatives are
classified non-retryable and burn the attempt immediately.

The order is: fix -> merge -> deploy -> verify one canary completes -> release
the backlog. Skipping the verification step is how a fleet gets a second
outage from the repair.

## Closing

1. Run the evidence the task named, at the scope it claimed.
2. `mac task close <id> --reason="..."` with the durable result — a test
   target, a commit, an observed state — not "done".
3. Leave partial work open across commits and sessions. An unchecked item is
   information; a checked one that was not verified is a lie the next agent
   will act on.
