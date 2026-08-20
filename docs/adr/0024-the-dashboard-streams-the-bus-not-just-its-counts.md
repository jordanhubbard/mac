# ADR 0024 - The dashboard streams the bus, not just its counts

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0018 (the task view is a graph under progressive disclosure),
  ADR 0022 (a gate returns a named decision)

## Context

The observability console shows the bus as four numbers. `AgentBusSection` is:

    streams_by_status:      Record<string, number>
    messages_by_status:     Record<string, number>
    chunks_in_window:       number
    chunk_bytes_in_window:  number

rendered as two status panels — "AgentBus streams" and "AgentBus messages" —
under Systems.

Counts answer *how much*. They cannot answer *what*, and on this bus the
content is the diagnostic. `mac fleet refresh-source` published to ten streams
on 2026-08-20 and **not one agent acted on it**; every host had to be updated
over ssh. From the console that failure is invisible: the publish incremented
the counters exactly as a working fleet would, because the counters measure
publication and nothing measures consumption.

The same shape produced a duplicate pull request (#405) when agents could not
see each other's `git.*` events, and it is why `task_77971dc7` — *nothing
consumes AgentBus* — is still open with a consumer still absent from
`worker.py`.

This mirrors what `FlowChart` already says about task states, in its own
docstring:

> a static table can tell you 360 tasks are blocked, but only this can tell you
> whether they are arriving or draining

The bus needs the equivalent move, one step further: not just the rate, but the
messages.

## Decision

Give the bus its own section on the top-level dashboard, showing live traffic
rather than only totals.

### 1. Its own section, at the top level

Bus traffic is not a subsystem statistic beside disk and telemetry. It is how
agents coordinate, and when it is silent the fleet is uncoordinated while every
other panel looks healthy. It gets a section an operator sees without
navigating to Systems.

### 2. Messages, with the fields that make one legible

Each row carries at least: time, sender, topic/event type, subject (the task or
agent it concerns), and status. Enough to answer "what is the fleet talking
about right now" without opening anything.

The payload itself is bounded in the row and expandable — the same progressive
disclosure ADR 0018 chose for the task graph, for the same reason: the common
question is answered by the summary and the detail is one interaction away.

### 3. Published AND consumed, distinguished

This is the requirement that makes the section worth building rather than
decorative. A message that was published and never read must look different
from one that was delivered.

Today's counters cannot express that distinction, which is exactly why a
publish to ten streams with zero consumers looked identical to a working
fleet. If the underlying data cannot yet answer it — consumers are the open
work in `task_77971dc7` — the section says so rather than implying delivery.

### 4. Live, bounded, and honest when it is not

Streaming under the console's existing rules:

- **Read-only.** `observe/tests/readonly.test.ts` asserts only GET and HEAD,
  every call through `src/lib/http.ts`, and no mutating verb anywhere in the
  source tree. A live stream must not become the first exception.
- **Honest.** `observe/tests/honesty.test.tsx` asserts a missing section says
  *unavailable* and never renders as `0`, and that "no transitions happened" is
  distinguishable from "transitions unavailable". A quiet bus and a broken
  stream are opposite facts and must not look alike — that distinction is the
  whole point here.
- **Bounded.** A busy fleet can produce more traffic than a browser should
  hold. The view keeps a bounded window and states what it dropped rather than
  silently truncating.

### 5. No new rendering dependency

`observe/package.json` declares exactly `react` and `react-dom`. A message list
needs no third; if one later proves necessary it is a separate decision with
its own justification.

## Consequences

- The failure mode that has cost the most this month — a bus everything writes
  to and nothing reads — becomes visible instead of being inferred from a task
  that did not happen.
- An operator can see coordination happening, which is currently only
  observable by querying `observability_events` by hand.
- Live traffic on a dashboard invites watching it. It is a diagnostic, not a
  feed; the section should make the *state* legible at a glance rather than
  requiring the operator to read every message.
- The published/consumed distinction depends on consumer-side data that does
  not exist yet. Building the view first is still worthwhile — it makes the
  gap visible — but it must not fabricate a delivered state it cannot observe.

## Alternatives considered

**Add message counts by topic to the existing panels.** Rejected: still
counts. It would show that `repo.update` was published ten times and nothing
about whether any agent acted, which is the question that mattered.

**Point operators at `mac admin agentbus read`.** Rejected as the answer on its
own: a CLI read is a deliberate act taken when you already suspect the bus.
The failure here is that nobody suspected it — the counters looked fine. A
dashboard section is seen without being sought.

**Wait for consumers to exist (`task_77971dc7`) before building the view.**
Rejected: that inverts the value. The view is what makes the absence of
consumers obvious, and today the absence is only discoverable by noticing that
an agent did not do something.
