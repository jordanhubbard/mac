# ADR 0014: Agent visibility is a communication boundary, not a dispatch gate

- Status: Accepted
- Date: 2026-08-17
- Supersedes: the dispatch-eligibility half of the agent-ownership work (#328)

## Context

Agents carry `visibility` (`private` | `shared`) and `owner_human_id`. Until
now the allocator treated `private` as a hard authorization gate: a private
agent was refused any task whose `created_by_human` was not its owner, and
`dispatch_preflight` mirrored that rule.

The stated rationale was reachability:

> a private worker often sits on its owner's own network, so "prefer not to"
> would mean placing work on a machine the fleet cannot reach

Two things are wrong with this.

**It authorized nothing.** Access to a fleet is boolean and is decided
*outside* mac: either you can reach the hub and the repository being
collaborated on, or you cannot. If you can, you can submit work — and you can
file it under any name you like, because `created_by_human` is a field on a
request, not a credential. Gating dispatch on it was a lock with the key taped
to it. In practice every worker in the reference fleet is "private" in the only
sense that matters, since one person's SSH keys, GitHub credentials and API
keys are what the whole fleet runs on.

**The reachability argument was already covered.** `AGENT_OFFLINE` rejects any
agent that is not heartbeating. An agent the hub can see is by definition an
agent the hub can reach; one it cannot see is already excluded. The visibility
gate added nothing on top.

Meanwhile the cost was real. Measured on the live fleet on 2026-08-17: every
bare-metal worker was `private`, most filed work carried no `created_by_human`
at all, and the hub sat with **18 ready tasks, 3 idle agents and 0
assignments** — a deadlock that could never drain, surfaced only as a
`dispatcher.v2.ready_capacity_mismatch_warning` nobody was reading. Preflight,
the tool built to answer "would this ever be claimed?", reported 4 eligible
agents for a fleet that had 7, and that answer was used to conclude the fleet
had no execution capacity at all. It had plenty; a canary ran on one of the
supposedly ineligible hosts minutes later.

This had also happened before. The docstring on `mac task reassign` records it:

> marking a worker private makes it refuse the entire existing backlog. That is
> not a hypothetical: doing exactly that took three of eight workers out of
> service.

A control that has to be undone by a repair command every time it is applied is
not a control.

## Decision

**`visibility` no longer participates in dispatch eligibility.**

It means what it should always have meant: a *communication* boundary. A
private agent is one the hub does not talk to anyone but its owner about,
unless the owner grants permission. Such an agent still collaborates with the
outside world the way any contributor does — through the git repository, its
code, its PRs and its issues. None of that is a statement about which problems
it may work on.

Concretely:

- `allocator._eligibility_rejections` no longer emits
  `AGENT_PRIVATE_TO_OTHER_OWNER`. The constant is retained, and stays in
  `AUTHORIZATION_REJECTIONS`, so rejections recorded before this date still
  read correctly.
- `dispatch_preflight.preflight` ignores `created_by_human`. The parameter is
  kept so existing callers and the HTTP body do not have to change.
- `mac task preflight --as-human` is accepted and ignored.
- `owner_human_id` and `created_by_human` are still recorded. They are useful
  for accountability — knowing who to ask about a task — and that is the whole
  of their job now.

## Consequences

- Unattributed work is claimable again. The 2026-08-17 deadlock cannot recur,
  and `mac task reassign` is no longer load-bearing for keeping a fleet running
  (it remains available for deliberate re-filing).
- Preflight stops under-reporting capacity, which was the failure mode it was
  built to prevent.
- Registering an owned agent still defaults it to `private`. That default is
  now about hub communication only and no longer idles hardware.
- Anyone who can reach the hub can direct any agent in it. That was already
  true; this ADR stops pretending otherwise. Genuine restriction belongs at the
  boundaries that actually enforce it — network reach, hub tokens, repository
  permissions — not in an allocator comparison of two strings the caller
  supplies.

## Alternatives considered

**Keep the gate and always record a filer.** Rejected: it treats a data-entry
omission as an authorization outcome, and the enforcement is still fictional.
Any caller can name any human.

**Make dispatch treat "no filer" as claimable by the fleet owner.** Rejected as
a half-measure: it fixes the observed deadlock without addressing why the gate
exists, and leaves the same trap for any second human added to the fleet.
