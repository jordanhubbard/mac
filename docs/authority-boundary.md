# Who owns which authority question

MAC has several primitives that all *look* like "is this allowed?" and answer
different questions. That ambiguity is not theoretical: it produced two
confirmed holes in one week, and a 34-route audit found that nothing at all was
blocking a non-admin token on the routes that relied on the wrong one.

This document fixes the boundary. It is descriptive of what the code does
today, and `tests/api/test_authority_boundary.py` pins the parts that must not
drift.

The framing is borrowed from `~/Src/literate-ai`'s
`docs/architecture/agent-ledger-boundary.md`, which draws the same line between
a ledger and a derivation engine:

> The ledger's customer or organizational consent **is not the execution grant**
> that authorizes a build, test, or publication step.

MAC is both systems at once, so the line has to be drawn *inside* it.

## The table

| Question | Owner | Not the owner |
| --- | --- | --- |
| Who asked for this work, and under whose organizational authority? | the task ledger — `actor`, `origin`, directives, approvals | — |
| May this principal reach this route at all? | **`_required_scope(method, path)`** | anything in the handler body |
| Is this principal confined to a tenant? | `TokenPrincipal.refuse_tenant_bound` | — it answers only this |
| Is this principal acting as itself? | `TokenPrincipal.assert_actor` | — |
| Does this call need an operator rather than a service? | `TokenPrincipal.require_admin` | — |
| May this agent execute anything at all on a host? | the allocator's execution-boundary gate (`AGENT_NO_EXECUTION_BOUNDARY`) | capability matching |
| Under what confinement does the work run? | the OpenShell policy, resolved per host and (optionally) per task | the task body |
| May this run escape confinement, once? | a lease-bound break-glass authorization | any token scope |
| What may the sandbox reach on the network? | the project's declared egress + the reviewed registry allowlist | anything the repo says about itself |
| What did the agent actually do? | `action_events`, `command_audit`, evidence blobs | — |

## The invariant

**Authorization is decided by `_required_scope`. Everything else narrows.**

A route's privilege is the scope it is given there. In-handler guards can refuse
*more* — a tenant-bound token, a different agent, a non-admin — but they cannot
supply privilege a route was never given. A route that needs to be operator-only
says so in `_required_scope`; it does not acquire that property by calling
something in its body.

The deliberate exception was the debug-terminal routes: they had to stay
reachable by a **worker acting on itself** — a blanket admin scope would forbid
the legitimate case — so they sat at `read`/`write` and were narrowed in the
handler. Scope alone therefore did not prove those routes safe; only a request
did, and writing this document's tests is what surfaced that, having first
asserted the tidier claim and watched it fail. Those routes are now gone
entirely, along with the handler gate and the `terminal_sessions` field they
fed. The *lesson* is not gone, which is why the tests still assert behaviour:
the next in-handler narrowing will look exactly as reasonable as that one did.

## The bus is a conversation, and joining one is not a privilege

The AgentBus reads (`/agents/{id}/agentbus/traffic`, `.../roll-call`) are
self-only: `assert_actor` binds the path agent to the bearer principal, because
an agent connects to the bus **as itself**. That rule is what makes `agent_id`
in a URL an identity claim rather than a parameter.

A front end holding a token cannot guess which agent it is, and guessing wrong
produces a 403 indistinguishable, in a browser, from the hub being down. There
were two ways to fix that and only one of them is acceptable:

- **Rejected:** widen `assert_actor` so a read token may name any agent. This
  would end the identity claim for every route that makes one, not just these.
- **Taken:** `GET /agentbus/identity`, which reports the binding the hub has
  **already made** for the presented credential — the caller's own agent id, or
  a plain statement that the credential is not an agent's.

`identity` is a mirror, not a key. It sits at `read` because it discloses
nothing a holder of the token does not already have: it names no other agent and
grants no access. A read token that has just been told "you are not an agent" is
still refused by the two reads above, and `tests/api/test_agentbus_console_identity.py`
asserts exactly that — the answer, and its uselessness as a credential.

The observability console therefore joins the bus by **holding an agent-bound
credential**, not by being exempted from the rule. See ADR 0025.

### How this was learned

`refuse_tenant_bound` was called `require_global_fleet`, which reads as "require
fleet-level authority". It is:

```python
if self.is_admin or self.tenant_id is None:
    return
raise AuthorizationError(...)
```

It refuses only **tenant-bound** non-admin tokens. An untenanted client token —
the ordinary `read`/`write` credential — passes untouched. It was relied on at
61 call sites.

An audit probed all 34 routes where it was the only in-handler gate below admin,
using the fact that FastAPI validates the request body *before* the handler runs
(so a `422` proves auth already passed):

```
REACHABLE past the guard with a non-admin token:  34 / 34
BLOCKED:                                           0
```

Two were real holes and are fixed:

- an ordinary `write` token could **open a debug shell on any fleet host and
  send it keystrokes** (PR #300);
- the same token could **create, update, delete and assign the OpenShell
  guardrail policy** every `--yolo` agent runs under — and then be refused when
  it tried to read back what it had just written (PR #303).

The remaining 22 have legitimate non-admin callers — bootstrap registers
machines, CI records integration findings, the `deploy` scope promotes runtime
deltas — so `write`/`deploy` is their correct level and they were never
vulnerabilities. The method was renamed in PR #305 so the assumption cannot be
made a 62nd time.

## Consent is not an execution grant

These are separate and must stay separate:

| | Answers | Carried by |
| --- | --- | --- |
| **Consent** | "this work was asked for, by someone entitled to ask" | the ledger: task, actor, origin, approval, directive |
| **Execution grant** | "this exact operation may run, here, now, with these resources" | scope + confinement policy + break-glass, per call |

A task existing in the ledger is not permission to run it on a host. An agent
holding a lease is not permission to escape its sandbox. The ledger's record of
*who wanted it* and the runtime's record of *what was allowed* answer different
questions and are stored separately on purpose.

The clearest live example is break-glass: an operator's organizational decision
to allow one host execution becomes a **lease-bound, single-use, audited**
authorization, projected onto exactly one assignment and stripped from every
other. Consent is durable and human; the grant is narrow, typed and expiring.

MAC enforces this more strictly than "strip it later": a task that tries to
carry `runtime.break_glass_authorization` in its own metadata is **refused at
creation** with *"break-glass execution metadata is control-plane-owned"*. The
assignment-time strip is the second line, not the first. (This document
originally claimed only the strip; the test found the stronger guarantee.)

## Trust tiers for anything a repository says about itself

Repository content is attacker-controllable — anyone who can open a pull request
can change it. So a repo's own statements are *proposals*, never grants:

| Tier | Source | Granted when |
| --- | --- | --- |
| `derived_trusted_registry` | the repository working tree (`.npmrc`, lockfiles) | it matches a small reviewed registry allowlist |
| `hub_declared` | control-plane state an operator set | it is well-formed |

This is why sandbox egress is declared on the **project**, not read from the
repository's own contract: `project_repositories` loads that contract from
`.mac/project.yaml` inside the checkout, which is repo content and belongs in
the untrusted tier.

## Current status — what is NOT true yet

Stated plainly, in the spirit of the document this borrows from:

- **`_required_scope` is a path-matching function, not a registry.** It is
  correct today and pinned by tests, but a new route inherits a scope by where
  its path happens to fall in a chain of `if`s. Nothing forces an author to
  make a decision.
- **22 routes still rely on `refuse_tenant_bound` alone.** That is correct for
  them — they have legitimate non-admin callers — but "correct by accident of
  who happens to hold a token" is weaker than "correct by declaration".
- **There is no derived explanation layer.** MAC has the forensic half —
  `action_events`, `command_audit`, evidence blobs — and no human-facing
  account of what was decided and why. literate-ai's boundary document is
  explicit that the raw journal is forensic evidence, not the default user
  interface.
- **Execution grants are not uniformly typed.** Break-glass is a typed,
  lease-bound record. Confinement is a policy file. Network reach is a projected
  contract. They are three shapes for one idea.

## References

- `~/Src/literate-ai/docs/architecture/agent-ledger-boundary.md` — the ownership
  framing, and the consent-versus-grant distinction
- `docs/structured-task-bodies.md` — the related question (should a task body be
  a component transition?) and why the answer was no
- `docs/openshell-sandbox.md` — confinement, policy delivery, per-repo egress
- `tests/api/test_authority_boundary.py` — the executable half of this document
