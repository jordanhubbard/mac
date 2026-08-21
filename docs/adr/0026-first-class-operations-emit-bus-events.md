# ADR 0026: Every operation on a first-class object emits a bus event

- Status: Proposed
- Date: 2026-08-21
- Decision owner: MAC fleet owner
- Related: [ADR 0013](0013-authoritative-hub-allocator.md) — the hub is the one
  authority, and therefore the one place that knows a transition happened

## Context

AgentBus carries messages between agents. It does not carry the fleet's own
work. Every topic present on the live bus, measured 2026-08-21:

    98  mac.reflect.request.v1        43  human.directive.v1
    86  peer.reply.v1                 33  mac.repo.update.v1
    55  peer.message.v1                8  mac.reflect.result.v1
    55  mac.repo.update.result.v1      5  fleet.coordination
     4  operator.recovery.directive    3  ops.gateway.redeploy
     2  coordination                   1  scope

There is no `task.*` topic. Nothing announces claimed, started, completed,
failed, or needs-review. The same is true of every other first-class object: a
project pausing, an agent going offline, a fleet being deployed, a human
directive being acted on — none of it is on the bus.

So **nothing can be told that anything happened.** Every observer polls, and an
observer that must ask is an observer that must already suspect.

### What polling-only cost, measured

- A cohort transaction sat non-terminal with a dead owner for **nine days**,
  blocking every deploy. Nothing announced it; it surfaced when someone read a
  journal by hand.
- Qdrant memory ingestion stopped on 2026-07-25 and was discovered **27 days
  later** by querying the collection.
- Three scheduled jobs failed **220 consecutive times each**. The failure
  notification used the channel that had just failed, so the only report of the
  outage was itself undeliverable.
- An operator session registered as an agent spent an entire working session
  discovering fleet state by re-running `mac task stats`. Eleven tasks were
  filed and claimed within the hour; the operator learned it only by asking
  again.

Each was found by looking. None was announced. A system in which every observer
polls cannot be surprised, and cannot surprise you either.

### The bus is write-only in both directions

Two streams addressed to that operator session were opened, closed within 20ms,
and never read:

    <worker-a> -> <operator-session>  mac.repo.update.result.v1  closed
    <worker-b> -> <operator-session>  mac.repo.update.result.v1  closed

`mac agentbus wait` blocks until the agent is messaged, and nothing in a CLI
session's loop calls it. So a registered CLI agent publishes and never receives.
Emitting more events without fixing that would only grow the pile of
closed-and-unread.

## Decision

**Every operation that changes a first-class object emits an AgentBus event.**

The first-class objects are the ones `mac` models — `project`, `task`, `agent`
— plus the fleet itself, the machines it runs on, and human user agents
represented through OpenClaw. If an operation is worth a CRUD verb, its
completion is worth an event.

### 1. The hub emits, not the caller

ADR 0013 makes the hub the one authority. It is therefore the only component
that knows a transition actually happened, as opposed to was requested. Emission
belongs at the same layer that writes the transition, in the same unit of work,
so an event cannot describe a change that did not land.

### 2. Emission may never fail the operation

A bus write that fails must not roll back a task transition. The operation is
the product; the event is the announcement. Failures to emit are recorded and
counted, never raised.

### 3. Topics are versioned and named for the object

`task.claimed.v1`, `agent.offline.v1`, `fleet.deployed.v1`. The existing bus
already versions its topics; this follows it rather than inventing a second
convention.

### 4. Addressed where there is an addressee, broadcast where there is not

A task transition is addressed to the task's owner and any watcher. A fleet
deployment has no single addressee and is broadcast. An event nobody can be
addressed by is still worth emitting, because the dashboard and the audit trail
read the stream rather than a mailbox.

### 5. Retention is designed in, not added after

**This is the condition on which the rest depends.** On 2026-07-15 the
`action_events` table reached 10.4 million rows and a 16GB database wedged the
hub. The observability store today holds 354,787 events, 305,540 logs and 49,247
metrics — and that is *before* adding an event per first-class operation.

So the retention policy ships with the emission, not after it: a bounded window
per topic class, a documented prune, and a test that asserts the prune actually
removes rows. An event stream with no expiry is a slow outage with a schedule.

### 6. A consumer must exist before the volume does

Emitting into a bus nothing reads reproduces the current failure at higher cost.
A CLI-registered agent needs a non-blocking way to drain what is addressed to it
— a pending count on ordinary output, or an explicit drain that prints and
closes — and that lands with, or before, the emission.

### 7. Delivery uses the protocols already implemented. No new one is invented.

The hub already speaks two standards, and both are in the tree today:

- **A2A 0.3.0.** The hub serves `/.well-known/agent-card.json` and accepts
  `POST /a2a`; the card describes it as accepting delegated tasks and running
  them on the fleet. `src/mac/a2a/` holds the protocol, service and card.
- **ACP.** `src/mac/acp/` holds a server, client, websocket transport, peer,
  permission and capability layers — the editor / coding-session standard.

So the connector question is already answered and the answer is "both, already".
What is missing is not a protocol but a wiring: **the A2A card currently
declares**

    "capabilities": {"streaming": false, "pushNotifications": false}

and those two are exactly the mechanism a subscriber needs. A2A specifies push
notifications; mac declines them. Turning that on, and giving ACP peers the same
stream, is how the events of §1–§5 reach anyone.

Two consequences of choosing the existing standards rather than a bespoke one:

- **An external agent needs no mac-specific client.** Anything that speaks A2A
  can subscribe to a fleet's work, which is the point of having adopted a
  standard in the first place.
- **The bus stays internal.** AgentBus remains the fleet's own transport;
  A2A and ACP are how it is *exposed*. This ADR does not propose replacing
  AgentBus with either, and does not propose a third thing.

An MCP endpoint is the obvious companion for coding-agent integration and is not
decided here: no `/mcp` route or MCP module was found, but that was checked from
a working copy 54 commits behind `main`, so its absence should be confirmed on a
current checkout before anyone builds one.

## Consequences

- The fleet becomes observable without polling. A dashboard, an operator
  session, or another agent can subscribe rather than ask.
- The failures above become announceable. A nine-day stuck transaction, a dead
  ingestion pipeline and a job failing 220 times are all events someone could
  have been told about.
- Event volume rises substantially, which is why §5 is a condition rather than a
  consequence.
- Two sources of truth for "what happened" — the bus and the existing history /
  action_events — unless they are reconciled. The ADR does not settle which is
  canonical; that needs deciding before implementation, or the fleet gains a
  second incomplete audit trail.
- A slow or wedged bus must not become a way to wedge task dispatch. §2 makes
  that structural rather than a rule to remember.

## Alternatives considered

**Emit only task events.** The immediate need, and too narrow. Agents going
offline, projects pausing and fleets deploying are exactly the transitions whose
silence cost days this session.

**Let callers emit.** Rejected by ADR 0013: a caller knows what it asked for,
not what the authority did. Two callers would disagree, and a failed write would
still be announced.

**Invent a delivery protocol for the events.** Rejected before it was started:
the hub already implements A2A 0.3.0 and ACP, and A2A already specifies push
notification. The work is to enable what exists (§7), not to design a third
protocol that every consumer would then have to learn.

**Keep polling, improve the dashboard.** Rejected: it makes the operator's
console better and leaves every other consumer — agents, sessions, automation —
with no way to learn anything. It also does nothing for the case where the
person is asleep.
