# ADR 0025 - The console joins the bus as an agent, and may only speak by name

- Status: **Proposed**
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0024 (the dashboard streams the bus, not just its counts),
  ADR 0019 (privilege is an ACL on a resource tree), PR #417 (the
  command-and-control dashboard was retired)

## Context

The terminal tab was a pseudo-terminal into a host. PR #417 deleted the HTTP
facade behind it on 2026-08-18 — 22,171 lines, including the xterm vendor tree
— because it was "the last surface where the hub UI could COMMAND rather than
observe". The PTY *capability* was deliberately kept: `worker_debug_terminal.py`
and the `DEBUG_TERMINAL_*` bus schemas survive, because a shell an operator asks
a **named agent** for over the bus fits the co-worker model. What went is the
route that reached past the bus into a machine.

That left a tab-shaped hole, and something better to put in it. AgentBus is a
fleet-wide conversation: every agent hears all of it, any agent can ask the bus
for a roll call of who is present and what each can do, and by convention
nobody answers until addressed by name. The hub has had the endpoints for this
since before #417 —

    GET  /agents/{id}/agentbus/traffic    everything being said, as this agent hears it
    GET  /agents/{id}/agentbus/roll-call  who is on the bus, and what each can do
    POST /agentbus/broadcast              announce a typed event to the fleet

— and **zero callers**. No CLI verb, no front end, nothing.

The reason they had no callers is the decision this ADR exists to make. Both
reads are self-only:

    principal.assert_actor(agent_id)   # an agent connects to the bus as itself

A human with a read token is not an agent. It cannot call either endpoint, and
worse, it cannot even *discover* that it cannot: it has to know its own agent
name to form the URL, and nothing on the hub would tell it.

## Decision

### 1. The console joins the bus as an agent, with one persona per human

It does not read the bus with a read token, and `assert_actor` is not widened
to let one name an agent in a path.

The rejected alternative is worth stating because it is the obvious one: let a
read-scoped token pass any `agent_id`. That would hand every holder of a read
token the fleet's entire conversation under a borrowed name, and it would make
"who said that" unanswerable the moment phase 2 lets the console speak. The
whole value of the bus is that `worker-3 pushed` means worker-3 pushed.

**One persona per human, not one shared operator identity.** `agent_operator`
already exists — a virtual persona with zero capabilities, currently held
because it claimed a task it could not execute. That history argues *for* the
shape (a bus participant that is deliberately not a worker) and against the
sharing: a single shared persona is fine for phase 1, where reading is
identical whoever asks, and wrong the instant phase 2 arrives, because every
operator would then speak as the same unattributable principal. Retrofitting
attribution after a shared mask is in use is the expensive direction; a
per-human persona costs nothing extra today. `TokenPrincipal.human_id` already
records which person a credential speaks for, so the binding has somewhere to
live.

A persona is an ordinary agent with no capabilities. It is never a dispatch
target, and the roll call shows it for what it is.

### 2. `GET /agentbus/identity` — the console asks who it is, and cannot ask who anyone else is

This is the only new hub route in phase 1. It reports the binding on the
credential the caller already presented: `agent_id`, `bus_participant`, and a
`reason` when the answer is no. It confers nothing — an unbound token is told
it is not a participant, not handed a persona.

It sits at the `agent` scope, alongside the reads it exists to enable, and not
at the `read` a GET would otherwise default to. A credential that cannot join
the bus has no business being told, by name, who is on it; the console renders
that refusal as "this session is not on the bus", using the hub's own words.

Admin authority is explicitly **not** a seat on the bus. Authority over the
fleet is not membership of it, and an admin persona would reintroduce exactly
the unattributable speaker the per-human decision avoids.

### 3. One message row, shared with ADR 0024

ADR 0024 gives the dashboard a live stream of bus traffic; this gives an
operator a view onto the same bus. They are two halves of one surface, and
designing them separately produces two views that disagree about what a message
looks like. The canonical row is `BusMessage` in `observe/src/lib/api.ts`,
mirroring the hub's traffic entry exactly:

| field | meaning |
| --- | --- |
| `cursor` | opaque, resumable position; on every row |
| `chunk.created_at` | when it was said |
| `from_agent_id` | who said it |
| `topic` | what channel it was said on |
| `addressed_to` | **who is expected to answer** — addressing, not access |
| `addressed_to_me` | whether this reader is one of them |
| `chunk.payload` | what was said, bounded in the row and expandable |

`addressed_to` is the field that carries the convention, so it is rendered
rather than hidden. An operator watching needs to see that a question was asked
of someone in particular and, therefore, that everyone else is right not to
have answered.

### 4. Phase 1 lands alone, and is read-only

`observe/tests/readonly.test.ts` asserts the console issues only GET and HEAD,
routes every call through `src/lib/http.ts`, and contains no mutating verb
anywhere in its source tree. Phase 1 keeps all three true: a Bus view with live
traffic and a roll-call panel, no compose box, no POST. `observe/tests/bus.test.tsx`
additionally asserts the view has no form, no textarea and no reference to
`/agentbus/broadcast`, so the compose box cannot arrive by accident ahead of the
decision below.

### 5. The invariant for phase 2: narrowed, named, and tested

Do not drop the read-only claim. Narrow it and test the narrower one:

> **The console observes state and participates in conversation. It never
> commands.**

Broadcasting a message is categorically different from `POST /dispatch/tick` or
`POST /agents/bulk`. What keeps it different is the convention that agents do
not act until addressed by name — the hub deliberately does not enforce that,
because enforcement would stop an agent volunteering the one fact that keeps
another from destroying work.

So the exception must be **named and enumerated**, not general. When phase 2
lands, the read-only test becomes an allowlist of exactly one endpoint
(`POST /agentbus/broadcast`, plus addressed messages) and continues to assert
that no other mutating verb exists in the tree. A test that merely stops
asserting "no POST" would let command-and-control back in the way it arrived
the first time: one reasonable-looking route at a time.

Phase 2 is not implemented here, and this ADR does not authorise it. It records
the decision it must be built against.

### 6. No bus-native PTY opener

Nothing on the hub opens a debug shell today. This work does not add one. A
shell an operator asks a named agent for over the bus is coherent with the
co-worker model, but it is an execution grant and needs its own decision;
`tests/api/test_authority_boundary.py` keeps the placeholder that says so.

## Consequences

- The two endpoints that had no callers now have two: the Bus view and
  `mac admin agentbus follow`. One backend, two front ends.
- A console session must hold an agent-bound credential to see the bus. A plain
  read token gets an explained refusal rather than an empty screen — the same
  honesty rule that governs every other panel.
- The `terminal_sessions` field goes with the routes. Leaving it would have
  meant the Fleet IDE rendering a Terminal tab that could never be non-empty:
  a panel that looks functional and is not. It is replaced by `bus_streams`,
  and the IDE's Terminal tab becomes its Bus tab.
- Per-human personas mean per-human credentials to provision. That is real
  operational cost, accepted because the alternative silently destroys
  attribution at exactly the moment attribution starts to matter.

## Alternatives considered

**Widen `assert_actor` so a read token may name an agent.** Rejected: it hands
every read token the whole conversation under a borrowed name, and it is
irreversible in practice once tokens are in circulation.

**Put the view in the Fleet IDE, which is already allowed to write.** Rejected
for phase 1 — the IDE reads `/dashboard/state`, which is assembled hub-side for
everyone, and the bus is self-only; the IDE would need the same persona
machinery anyway. It remains the right home for phase 2's compose box if the
narrowed invariant above proves too fine a distinction to hold in the console.

**One shared operator identity.** Rejected: see §1. Adequate for reading,
destructive for speaking, and the migration between them is the expensive part.

**Leave the terminal tab and add the bus somewhere else.** Rejected: the tab is
backed by routes that no longer exist. Keeping it would preserve a screen that
cannot work, which is worse than an absent one.
