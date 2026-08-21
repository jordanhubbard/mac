# Reaching an agent that is already working

Until now a message reached an agent *between tasks*. The executor runs the
coding CLI as one captured subprocess — `_run_captured(argv, cwd, timeout)` —
with no inbound channel, so once a task starts, nothing can reach it. A
correction arrived after the mistake was finished.

`mac agentbus wait` closes that gap. It is a small addition on top of AgentBus,
not a new transport.

## The mechanism

Three parts, none of them exotic:

1. **A blocking read of the agent's own inbox.** `mac agentbus wait <agent_id>`
   blocks until someone messages that agent, prints what arrived, and exits.
2. **The agent runs it as a background task of its own harness.** The agent
   keeps working in the foreground; the harness surfaces the completion between
   its work steps.
3. **A convention that the agent restarts the watcher** after acting, passing
   the previous `next_cursor`.

The harness is what makes this passive. A blocking wait in the foreground would
stop the agent from working — which is the thing being avoided. Run as a
background task, the message surfaces between steps without interrupting the
step in progress.

## Inbox, not stream

AgentBus already had `/agentbus/streams/{id}/events`, which answers *"what is
new in this conversation"* and needs a stream id. An agent mid-task does not
know which stream a correction will arrive on — that is what makes it a
correction. So the inbox answers the other question: *"has anyone said anything
to me"*, across every stream the agent can see.

Membership is the rule the bus already enforces: direct recipient, or a member
of a group stream. Two exclusions matter:

- **the agent's own messages** — a watcher woken by its own writes would spin
  instead of working;
- **other agents' conversations** — an inbox that leaked them would be
  surveillance rather than coordination.

The endpoint (`GET /agents/{id}/agentbus/inbox`) is self-only, carrying the
`agent` scope like the other worker control paths: an agent may watch its own
inbox and no other's.

## The cursor is the load-bearing part

`agentbus_chunks` has no global monotonic key — only `UNIQUE(stream_id,
sequence)` — so `sequence` is per-stream and would interleave two conversations
incorrectly. The inbox orders by `(created_at, id)` and its cursor is that pair.

Every result carries `next_cursor`, **including on timeout**. That is what makes
a restarted watcher safe: it resumes exactly where the previous one stopped, so
a message landing between rounds is not skipped.

A malformed cursor is ignored rather than applied. This is deliberate and was
found by a test: a corrupted value like `not-a-cursor` sorts above any timestamp,
so treating it as a bound silently hid the entire inbox. A watcher must
**over-deliver, never under-deliver** — a duplicated message is noise, a missed
correction is the failure the whole mechanism exists to prevent.

## Using it

```console
# One shot: block up to 5 minutes for a message.
mac agentbus wait agent_1234 --timeout-seconds 300

# Resume without replaying what was already seen.
mac agentbus wait agent_1234 --after-cursor "$PREV_CURSOR"
```

The intended shape inside a coding agent's harness, where `<background>` is
whatever that harness calls launching a task it will be notified about:

```
<background> mac agentbus wait $MAC_AGENT_ID --after-cursor "$CURSOR"
... keep working ...
(harness surfaces the watcher's output between steps)
... act on the message, then restart the watcher with the new next_cursor ...
```

Sending a correction is the existing bus:

```console
mac agentbus open  <sender> <recipient> --stream-id correction-1
mac agentbus append correction-1 <sender> --payload '{"text":"stop, wrong file"}'
```

## Not every consumer has a background slot

`wait` assumes a harness that can run a task in the background and surface it
between steps. An interactive CLI session registered as an agent has no such
slot, and the consequence was measured on 2026-08-21: two
`mac.repo.update.result.v1` replies were addressed to a registered session at
03:15Z and 03:21Z, opened and closed within ~20ms, and nothing ever surfaced
them. That session published to the bus all day and received nothing from it.

Two things were wrong at once, and both are fixed:

- **`wait` did not work in hub mode at all.** `RemoteDispatch` wrapped the
  entire agentbus surface *except* the inbox, so `mac admin agentbus wait`
  answered `not yet supported in hub mode` — for every agent in the fleet,
  because hub mode is the only mode a fleet agent runs in.
- **Blocking was the only shape offered.** So the inbox gained a non-blocking
  half:

```console
# How much is waiting? Returns at once.
mac agentbus pending agent_1234

# Take it. Also returns at once, whether or not anything was waiting.
mac agentbus drain agent_1234

# Look without consuming.
mac agentbus drain agent_1234 --peek
```

`drain` keeps the consumed position **at the hub**
(`agentbus_consumer_cursors`, topic `agentbus.inbox`), which is the difference
that matters for a session that exits between turns: `wait --after-cursor`
requires the caller to carry its own bookmark, and a session with nowhere to
keep one either re-reads everything or misses messages. Both spell the same
cursor, so the two can be interleaved.

Draining does **not** close the streams it read. On this bus, closing is the
sender's statement that a conversation is over, and it is what `agentbus
request` waits on to collect its reply; a recipient that closed an incoming
stream would destroy the channel it is supposed to answer on.

The count is cheap enough to hang off ordinary output, which is the point —
`mac agent show <id>` carries `pending_inbox`, `mac agent list --inbox` carries
`pending_inbox_count`, and `mac task show <id>` carries the owner's. An operator
who never learns the bus exists still sees that someone answered them.

## Task lifecycle is on the bus

A topic census on 2026-08-21 found eleven live topics fleet-wide — reflect
requests, peer messages, repo updates, human directives — and no `task.*` among
them. The fleet's entire subject matter was missing from its own bus, so nothing
could subscribe to it and every observer polled, including the operator: eleven
tasks were filed and claimed within an hour and the operator learned it only by
re-running `mac task stats`.

Every task transition now publishes an addressed record:

- **Topic** — `task.<state>`, derived from `TaskState` rather than hand-mapped,
  so a new state cannot ship without a topic.
- **Payload** — `mac.task.lifecycle.v1`, validated at publish time like every
  other registered schema. It carries the true `actor` of the transition.
- **Addressed to** — the task's owner, plus any agent id listed under the task's
  `metadata["watchers"]`. An operator that files a task and wants to hear about
  it adds itself there.
- **Sender** — the hub's existing virtual operator persona. The hub has no agent
  row of its own, and most transitions are performed by actors (`human`,
  `allocator`, `outbox`) that have none either.

The publication hook is the transition outbox, which already enqueued a
`task.lifecycle` row for every transition and then dropped it. That seam matters:
the row is written inside the transition's transaction and processed after it
commits, so a published record can never describe a state the database does not
hold. Publication is best-effort in the other direction too — a bus failure
never fails a transition that already committed, and never stalls the outbox
rows queued behind it.

One subtlety worth knowing if you touch this: a terminal or blocked transition
*releases the owner*, and the outbox is drained after that commits. A publisher
reading the owner off the task would find `None` on exactly the events an
operator most needs — "your task failed", "your task was blocked". So the
audience is resolved inside the transition's own transaction, where the outgoing
owner is still on the row, and travels with the outbox row.

## What this does not do

It does not interrupt the current step. The message surfaces at the harness's
next step boundary, which is a property of the harness, not of MAC. An agent
inside a long single tool call will not see it until that call returns.

It also does not make the agent obey. Delivery is a channel; whether a
correction is acted on is a matter of the agent's instructions.

## Provenance

The design is taken from `~/Src/AgentRadio` (Coral Protocol), whose
`wait_for_mention.sh` is a blocking poll loop the agent launches with
`run_in_background=true`, with a CLAUDE.md convention telling it to restart the
watcher. Their reported result on 124 codebase-understanding tasks is 62.1%
accuracy with background listening versus 51.6% blocking.

That number is theirs, from their benchmark, and is a reason to try this — not
evidence that it works here. MAC keeps its own bus: AgentBus streams are bound
to tasks and leases and audited into `action_events`, none of which a standalone
message server provides. What was adopted is the *delivery mode*, which is the
part they measured.

Worth measuring on this fleet before believing it. `mac task generator-yield`
already reports completion yield by origin, and MAC's own data is a caution:
`direct_task` (plain human free text) completes at 20.0% while machine-originated
work runs 0–9.6%, so communication mode may not be the binding constraint here.
