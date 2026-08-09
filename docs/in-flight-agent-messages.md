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
