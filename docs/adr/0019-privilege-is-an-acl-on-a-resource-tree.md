# ADR 0019 - Privilege is an ACL on a resource tree, not a bag of scopes

- Status: **Accepted; immediate adoption, verified adversarially post-deploy**
- Date: 2026-08-20 (accepted 2026-08-20)
- Decision owner: MAC fleet owner
- Related: ADR 0013 (one authoritative hub allocator), ADR 0014 (visibility is
  a communication boundary, not a dispatch gate), ADR 0016 (agents decide what
  a task needs)

## Context

A sandboxed executor cannot file a child of the task it is running. Hub
credentials are stripped from the sandbox (`_HOST_ONLY_HUB_CREDENTIALS`) with
a sound rationale — a fleet worker bearer would let the sandbox heartbeat,
claim work, mutate the ledger and sign evidence as the host identity. But the
enforcement is all-or-nothing, so a narrow, obviously-safe action is denied
along with the dangerous ones.

The obvious fix is a narrower token. Attempting to specify one exposed that
there is no coherent model to be narrow *within*.

### The audit

**Nine scopes, one flat namespace.** `KNOWN_SCOPES = {admin, agent, deploy,
dispatch, read, roles, secret, workflow, write}`. They are not ordered, not
nested, and not derived from anything. `read` is a verb, `secret` is a
resource, `agent` is a principal kind, `workflow` is a subsystem, `dispatch`
is an operation. Four different taxonomies in one set.

**The gate is a 204-line ordered if/elif chain.** `_required_scope(method,
path)` maps path prefixes to a single scope across 33 branches, and its own
docstring says "THIS is the authorization gate". Being ordered, an early broad
`startswith` shadows a later narrow one, and nothing detects that. The
distribution is lopsided: `agent` 8 routes, `admin` 8, `read` 5, `secret` 3,
`deploy` 2, and `write`, `workflow`, `roles`, `dispatch` **one route each**.

**Implicit grants that are invisible at the call site.** In `has_scope`:
`admin` returns true for everything, and `write` silently also grants `roles`
and `workflow`. So `write` — which guards exactly one route — is really three
domains. Any least-privilege reasoning that starts from `write` is wrong
before it begins.

**Bindings are four independent scalars, checked ad hoc.** `TokenPrincipal`
carries `tenant_id`, `agent_id`, `human_id` and `client_id`, each enforced by
a different in-handler call (`assert_tenant`, `assert_actor`,
`refuse_tenant_bound`, `require_admin`). They do not compose, and there is no
statement anywhere of which combinations are meaningful.

**There is no resource-level binding at all.** `_assert_task_actor(principal,
task_id, claimed_agent_id)` accepts a `task_id` and never reads it:

    def _assert_task_actor(principal, task_id, claimed_agent_id) -> None:
        principal.assert_actor(claimed_agent_id)

Authority is per-agent, never per-task. A function whose name promises
resource scoping and silently does not enforce it is worse than its absence,
because callers reasonably assume it works.

**The vocabulary is duplicated and already divergent.** `DEFAULT_SCOPES` is
defined twice — `("dispatch", "read", "write")` in `client_principals.py` and
`("read", "write", "dispatch")` in `client_login.py` — plus `WORKER_SCOPES`,
`ELEVATED_SCOPES`, `LIFECYCLE_SCOPES` and `DREAM_SCOPES` as separate,
unrelated notions of "scope". More than twenty distinct `MAC_*_TOKEN` /
`*_KEY` environment names carry credentials.

None of this is one mistake. It is the shape of a model that was extended one
route at a time.

## Decision

Replace scopes with **access control entries on a resource tree**, with
inheritance, modelled on filesystem ACLs.

### 1. Resources form a hierarchy with paths

    /fleet
    /fleet/project/<name>
    /fleet/project/<name>/task/<id>
    /fleet/project/<name>/task/<id>/evidence
    /fleet/agent/<id>
    /fleet/machine/<id>
    /fleet/secret/<name>
    /fleet/workflow/<id>

Every authorizable thing has exactly one canonical path. A route's requirement
becomes *(resource path, permission)* — data, not a branch in a chain.

### 2. Permissions are a small, ordered set

    read      observe the resource
    append    add to it without altering what is there (evidence, annotations,
              comments) — the operation an executor needs
    create    create children beneath it
    update    modify the resource's own fields
    write     replace or DELETE the resource itself
    stop      abort in-flight work and park the resource
    start     return a stopped resource to the queue
    control   lifecycle: claim, heartbeat, lease, transition
    grant     change the ACL

Ordered by strength but NOT implicitly implied: a grant of `write` does not
confer `control`. Implicit implication is exactly what made `write` mean three
things. Where a role needs several, it is granted several, visibly.

`update` is split out of `write`, and `stop`/`start` out of `control`, because
the common operator case — correcting a task's scope (ADR 0020) — should carry
neither the destructive nor the lifecycle ones. Someone fixing a bad task
description should not thereby be able to delete the task, nor to claim work
and impersonate a worker's lifecycle. `control` is what an EXECUTOR needs;
`stop`/`start` are what an OPERATOR needs.

### 3. Principals are users; roles are groups

A principal (worker, client, human, sandbox) holds ACEs directly and through
role membership, exactly as a filesystem user accumulates rights through
groups. `admin` stops being magic: it is `grant` at `/fleet`, and it appears
in listings like any other entry.

### 4. Inheritance, with the filesystem's rules

An ACE on a node applies to its descendants unless a descendant overrides it.
Grant `read` at `/fleet/project/mac` and it holds for every task in that
project, including tasks created later — the property that makes this usable
at 6,849-tasks scale.

Two rules keep it predictable:

- **Longest path wins**, deterministically. Not evaluation order. The current
  ordered chain has no defined behaviour when two prefixes both match; this
  does.
- **Deny is explicit and wins over any inherited allow at the same or shorter
  path.** Needed to carve one task out of a project-wide grant.

### 5. Deny by default, and no invisible grants

A path with no matching ACE is denied. `has_scope`'s implicit `write` →
`roles`/`workflow` bridge is deleted, not reproduced.

### 6. The route table becomes data

`_required_scope` becomes a declarative table from route to
*(resource template, permission)*, so the full privilege surface can be
enumerated, diffed in review, and tested exhaustively. A route with no entry
fails closed. The audit above required reading 204 lines of control flow to
learn what is currently enforced; that should be a query.

### The case that prompted this

The sandboxed executor gets, for the lifetime of its lease:

    allow  /fleet/project/mac/task/<id>            read, append, create

It can annotate its own task and file children of it. It cannot heartbeat
(`control`), cannot claim other work, cannot touch another task, cannot read a
secret, and holds nothing at `/fleet`. That is the least-privilege credential
the previous discussion tried to express as a new special case; here it is an
ordinary ACE, and it needs no new deny-list exception.

## Consequences

- Least privilege becomes expressible. Today the narrowest useful credential
  is a fleet worker token, which is why the sandbox gets none.
- "What can this principal do?" and "who can reach this task?" become
  queries against ACEs rather than an audit of control flow.
- **This migration can fail open, and the accepted mitigation is adversarial
  verification rather than dual-path shadowing.** See the acceptance record
  below for the reasoning and the required sequencing.
- Inheritance means a grant high in the tree is powerful and easy to make
  carelessly. Listing effective permissions for a principal, and showing where
  each is inherited from, is required — not optional tooling.
- Nine scopes on ~25 routes is small enough to be mapped exhaustively, which
  is what makes the migration tractable now. It will not stay that way.

## Acceptance record

Accepted 2026-08-20 by the fleet owner, for immediate adoption at P0.

### The dual-path requirement was considered and deliberately not taken

The Proposed version required shadow evaluation: run both gates, enforce the
old one, and move only when the disagreement log was empty. That was rejected
on a risk assessment specific to this fleet:

- The fleet is firewalled and not reachable by external actors.
- Tailscale routing provides further isolation *within* the firewall.

So the window in which a fail-open defect could be exploited by an outside
party is not a window that exists here, and paying dual-path's cost to close
it is not warranted. Recorded explicitly because "we skipped the safety rail"
is exactly the decision a future reader will want the reasoning for.

### What replaces it

Implement the architecture together with a test suite that actively probes the
boundaries of the new mechanism — deliberately attempting the operations that
must be refused, and confirming that each fails closed rather than merely
confirming that permitted operations succeed. A suite that only proves the
allowed paths work cannot detect fail-open; that asymmetry is the entire risk
here, so the tests must be written from the refusal side.

### Sequencing, which is load-bearing

Adversarial verification runs **after deployment covers every agent**. A
partially deployed fleet produces false results in both directions: an agent
still on the old gate can pass a probe that the new gate would fail, and can
fail a probe that the new gate would pass. Neither outcome is informative, and
both are easy to misread as evidence. Deploy fleet-wide first; probe second.

### One thing the isolation argument does not cover

The perimeter argument is about *external* actors, and it holds. But the threat
model that produced this ADR is an agent *inside* the perimeter: a sandboxed
executor that is confused, is running a prompt-injected instruction, or is
simply buggy. A firewall does nothing about that, and neither does Tailscale.

This does not change the decision — it sharpens what the adversarial suite must
cover. The probes that matter are the ones launched from a principal that
already holds a valid, narrow credential and tries to exceed it: a task-scoped
grant reaching for another task, for `control` on its own task, for a secret,
or for anything at `/fleet`. Probes from an unauthenticated outsider are the
easy case and the one the network already answers.

## Alternatives considered

**Add one narrow scope for the sandbox case.** Rejected: it is the tenth
entry in a namespace that is already incoherent, and it does nothing for the
missing resource binding — a `task_author` scope still could not say *which*
task without inventing a parallel mechanism.

**Add a `task_id` binding beside `agent_id` and `tenant_id`.** Rejected: it
makes a fifth independent scalar and a fifth ad-hoc check. The problem is that
bindings do not compose; adding one more does not fix that.

**Adopt an off-the-shelf policy engine (OPA/Cedar).** Rejected for now: the
gain is expressiveness the fleet does not yet need, at the cost of a second
language and a runtime dependency in the request path. Revisit if ACEs alone
prove insufficient — the resource-path model above is compatible with both.

**Leave it and document the traps.** Rejected. The traps are not incidental:
a function that ignores its `task_id`, and a `write` scope that silently
carries two more domains, are the kind of defect that is discovered by
something being permitted that should not have been.
