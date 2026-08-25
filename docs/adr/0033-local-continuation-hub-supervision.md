# ADR 0033: Agents continue locally under independent hub supervision

- Status: **Accepted**
- Date: 2026-08-25
- Decision owner: MAC fleet owner
- Related: [ADR 0013](0013-authoritative-hub-allocator.md) — the hub remains the
  task and lease authority
- Related: [ADR 0023](0023-one-skill-source-many-harness-plugins.md) — only the
  hub sends an addressed stall nudge
- Related: [ADR 0026](0026-first-class-operations-emit-bus-events.md) — agents
  announce coarse progress; they do not stream private reasoning
- Related: [ADR 0032](0032-cli-session-hooks-not-tmux.md) — harness hooks are the
  live I/O boundary for interactive CLI sessions

## Context

A hub-only recovery nudge solves duplicate supervision: peers do not all prod a
silent session. It should not also make the hub responsible for every ordinary
next turn. A healthy worker already knows that its previous iteration finished
and can schedule its next one without a round trip.

The opposite extreme is also wrong. A perpetual local mind that chooses work,
keeps working after losing its lease, or treats its own timer as authority can
conflict with another agent and spend tokens while explicitly waiting for CI,
review, or a human.

The useful part of a persistent microharness is local continuation and bounded
backoff. The useful part of MAC is independent authority and recovery.

## Decision

**A healthy agent schedules its own next iteration. Every iteration re-enters
through the hub's heartbeat, hold, claim, task-state, and lease checks.**

The local continuation policy has four outcomes:

1. Real work or progress schedules an immediate next iteration.
2. An empty iteration schedules exponential backoff with a small cap.
3. A transient iteration failure schedules the same bounded recovery backoff.
4. An explicit hub hold is waiting, not progress: local wakes stay at the cap
   until the hub releases the hold.

The worker's timer is a wake-up mechanism, not authority. It does not preserve a
claim across iterations, select work outside the ledger, edit a running task, or
override a hold. The existing lease renewal thread and subprocess timeout remain
the local watchdogs while an executor is active. If the worker process crashes,
hangs past that timeout, loses its lease, or stops producing progress, the hub's
independent liveness and ADR 0023 stall recovery still apply.

Workers emit `task.progress` only at coarse execution boundaries:
`execution_started` and `execution_finished`. Tool thoughts, chain-of-thought,
and per-token activity are never published. These events let the hub distinguish
productive work from a heartbeat-only stall without creating a firehose.

Interactive Claude, Codex, Cursor, OpenCode, and Pi sessions use the same policy
at their own turn boundary once ADR 0032 hooks are installed. The fleet worker
implementation does not wrap those CLIs in tmux or introduce another session
protocol.

## Consequences

- Productive agents continue without waiting for a hub nudge.
- Empty queues and transient faults do not hot-poll.
- A durable hold suspends speculative work and token use.
- The hub can still recover a dead local loop because liveness, ownership, and
  addressed nudging remain independent of that loop.
- Local continuation is a small deterministic state machine with focused tests,
  rather than prompt language every harness may interpret differently.

## Not this ADR

- Autonomous interest selection while idle.
- Peer-to-peer stall nudges.
- Removing leases, heartbeats, subprocess timeouts, or hub stall detection.
- Streaming private model reasoning to AgentBus.
- Implementing the harness hook adapters accepted by ADR 0032.
