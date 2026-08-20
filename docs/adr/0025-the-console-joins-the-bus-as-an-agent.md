# ADR 0025 - The console joins the bus as an agent

- Status: **Accepted**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0024 (the dashboard streams the bus, not just its counts),
  PR #417 (the command-and-control dashboard was retired),
  `docs/authority-boundary.md`

## Context

The console had a terminal tab: a pseudo-terminal into a host, fronted by five
HTTP routes and an xterm vendor tree. #417 deleted that on 2026-08-18 — 22,171
lines — because it was "the last surface where the hub UI could COMMAND rather
than observe". The capability was explicitly **not** deleted:
`worker_debug_terminal.py` and the `DEBUG_TERMINAL_*` AgentBus schemas survive,
because a shell an operator asks a **named agent** for over the bus fits the
co-worker model. What went is the HTTP facade that bypassed the bus.

The tab is still worth a screen; a remote shell is not. AgentBus is a
fleet-wide conversation any agent can join: it hears everything being said, it
can ask the bus for a roll call of who is present and what each can do, and by
convention nobody answers until addressed by name. Four endpoints already exist
on the hub with **zero front ends** — traffic, roll call, broadcast, and the
cursored stream tail.

Two things had to be decided before any of that could be rendered.

### The identity problem

Both read endpoints are self-only:

```python
principal.assert_actor(agent_id)   # an agent connects to the bus as itself
```

A human holding a read token is not an agent and cannot call them. So the
console must join the bus **as** an agent — and a browser cannot discover which
agent a token is bound to, because a token does not carry that in any readable
form. Guessing produces a 403 that, from a browser, is indistinguishable from
the hub being down.

### The read-only problem

`observe/tests/readonly.test.ts` asserts the console issues only GET and HEAD,
routes every call through `src/lib/http.ts`, and contains **no mutating verb
anywhere in its source tree**. A view that sends to the bus is a mutation. That
test exists because the last write-capable UI is what #417 deleted, so the
first POST into that tree is not a detail to be slipped in.

## Decision

### 1. One shared operator persona, not one identity per human

The console joins the bus as a **bus participant that is deliberately not a
worker** — the shape `agent_operator` already has: a "virtual operator persona
with zero capabilities". It is currently HELD, because it claimed a task it
could not execute. That history argues *for* this shape, not against it: what
went wrong was that a non-worker was dispatchable, and zero capabilities plus a
dispatch hold is exactly the fix.

**One shared persona, not one per human.** Considered and rejected: a persona
per operator gives per-human attribution on the bus, which is real value, but
it puts a *registered agent* behind every console session. The roll call is a
roster of who can take work; filling it with humans who can take none makes the
one question it answers harder to answer. Attribution for phase 2's messages
belongs in the message (`human_id` on the payload), not in the roster.

Consequence, stated plainly: **phase 2 broadcasts will be attributed to the
persona, and the human must be named in the payload.** A persona whose messages
cannot be traced to a person would be worse than the PTY it replaced.

### 2. `GET /agentbus/identity` — report the binding, do not widen the rule

The console asks the hub which agent its credential is:

```
GET /agentbus/identity
→ {"schema": "mac.agentbus.identity.v1",
   "agent_id": "agent_operator", "joined": true, "reason": ""}
```

and for a credential that is not an agent's:

```
→ {"agent_id": null, "joined": false,
   "reason": "this token is not bound to an agent, so it cannot join the bus…"}
```

**`assert_actor` is not widened.** This reports a binding the hub has already
made; it is a mirror, not a key. It sits at the `read` scope because it
discloses nothing a holder of the token does not already have — it names no
other agent — while the traffic and roll-call reads stay at `agent` and still
refuse a token that is not the named agent.
`tests/api/test_agentbus_console_identity.py` asserts both halves: the answer,
and its uselessness as a credential.

The alternative — letting a read token name any agent it likes — was rejected
outright. `agent_id` in a URL would stop being an identity claim on *every*
route that makes one, not just these two.

### 3. Phase 1 lands alone, and changes no invariant

The Bus view is a **read**: live traffic with the speaker, whether the message
was addressed and to whom, and a roll-call panel of present agents with their
capabilities. No compose box. No POST. `readonly.test.ts` keeps asserting the
console never mutates, and it stays true — unchanged, still passing, now
covering one more view.

### 4. The invariant phase 2 must meet, stated now

The read-only claim is not dropped when compose lands. It is **narrowed**, and
the narrower one is what gets tested:

> **THE CONSOLE OBSERVES STATE AND PARTICIPATES IN CONVERSATION; IT NEVER
> COMMANDS.**

Broadcasting a message is categorically different from `POST /dispatch/tick` or
`/agents/bulk`. What keeps it different is the convention that agents do not act
until addressed by name — the hub deliberately does not enforce that, because
enforcement would stop an agent volunteering the one fact that keeps another
from destroying work.

So the exception must be **named and tested**, not merely permitted. When phase
2 lands, `readonly.test.ts` becomes an allowlist of exactly one mutating call —
`POST /agentbus/broadcast` and addressed messages — and asserts that no other
mutating verb exists in the tree. An allowlist that grows is visible in a diff;
"the console can write now" is not.

### 5. Addressing is rendered as addressing, never as privacy

Point-to-point messages on this bus are **not private**. `addressed_to` names
who is expected to answer. The view renders it as `→ names` and the CLI carries
the same field; neither uses any treatment that reads as confidentiality,
because that would misdescribe the bus it is showing.

### 6. One backend, two front ends

The same two reads back `mac admin agentbus follow` (the `tail -f` the CLI was
missing — `wait` exits on the first message and must be re-invoked with
`--after-cursor`) and `mac admin agentbus roll-call`. A divergence between what
the terminal shows and what the console shows is then a bug in one of them,
rather than two products drifting.

This is also why ADR 0024 and this decision are paired: 0024 shows what agents
are saying on the dashboard, this lets an operator eventually say something
back. Designed separately they would produce two views of the same bus that
disagree about what a message looks like.

## Consequences

- The PTY residue goes with this work rather than as a follow-up: the dead
  helpers, the `DashboardTerminal*` request models, and the `terminal_sessions`
  field. The field mattered most — the Fleet IDE rendered a Terminal tab from
  it, and with the creation routes deleted that panel could never be non-empty.
  It is now the **Bus** tab, reading `agentbus_streams`.
- `agent_operator` needs its hold reviewed: as a zero-capability, dispatch-held
  persona it is correct, and it should not be released as a worker.
- Phase 2 is gated on the allowlist above existing, not on this ADR alone.
- The console now needs an **agent-bound token** to show the Bus view. A plain
  read token gets a stated "this session is not on the bus" rather than an empty
  conversation, because an empty conversation reads as a quiet fleet — the exact
  failure ADR 0024 exists to make visible.

## Alternatives considered

**Put the compose box in the Fleet IDE instead, which is already allowed to
write.** Not rejected — deferred. It remains the cheaper option for phase 2 and
this ADR does not foreclose it. But the read half belongs in the console
regardless, and splitting the two halves of one conversation across two
products is the divergence ADR 0024 warns about.

**Let a read token call the bus reads by widening `assert_actor`.** Rejected.
See §2: it would end the identity claim everywhere, to save one endpoint.

**Rebuild the PTY over the bus as part of this work.** Rejected. #417's
carve-out is precise: address a **named agent over the bus**. A new HTTP route
that reaches past the bus into a machine would undo #417 while looking like
progress. Nothing here opens a session; the bus-native opener, if it is ever
wanted, is its own decision with its own authority argument.
