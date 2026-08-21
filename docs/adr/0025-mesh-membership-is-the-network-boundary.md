# ADR 0025 - Mesh membership is the network boundary for the hub API

- Status: **Proposed** — design only. Nothing in this ADR is implemented.
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0019 (privilege is an ACL on a resource tree), ADR 0013
  (authoritative hub allocator)

## Context

The operator's stated goal is that all agent-to-agent traffic ride the fleet
mesh "for security". Two of the three gaps behind that goal were wiring gaps
and are closed: the self-hosted headscale pre-auth key now lands in the secrets
vault, and `mac admin secret get` gives a non-agent caller an audited way to
fetch it. This third one is not a wiring gap. It is a change to the network
security model, and this ADR is the design pass it needs before anyone writes
the code.

### What the boundary actually is today

The hub's bind address is chosen at fleet-config time and threaded through the
deploy pipeline unchanged:

    deploy/deploy-mac-fleet.sh:1352   control_bind_host defaults to
                                      "0.0.0.0" for the hub agent and
                                      "127.0.0.1" for every other node
    deploy/deploy-mac-fleet.sh:8382   -> MAC_DEPLOY_CONTROL_BIND_HOST
    deploy/fleet-node-install.sh:287  -> CONTROL_BIND_HOST
    src/mac/deploy_env.py:359         -> MAC_BIND_HOST in mac.env
    deploy/fleet-node-install.sh:11038 -> uvicorn --host "$MAC_BIND_HOST"

So a hub listens on every interface the host has. Whether that is the mesh, the
office LAN, or a public NIC is a property of the machine, not of anything the
fleet configuration states or checks.

`hub_url` is independently configured — a mesh IP, a cluster DNS name, or
loopback, whatever was typed at setup. Nothing compares it to
`network.provider`. A fleet can be configured `provider: headscale`, stand up a
mesh, and still have every agent talking to the hub over its LAN address, and
no part of the system would notice or say so.

### What is already protecting it

Not nothing, which matters for judging urgency. `mac-853j`
(`src/mac/api.py:4419`) refuses to start a non-loopback hub with no tokens
configured, precisely so that `/secrets/{id}/reveal` is not reachable
unauthenticated from the network. Every route carries a scope requirement, and
`/secrets/*` is rate-limited per principal.

The current posture is therefore: **reachable from anywhere the host's NICs
reach, authenticated by bearer token**. The gap this ADR addresses is that the
network layer contributes nothing — a stolen token works equally well from the
mesh and from a coffee shop.

## Decision

*Proposed.* Make mesh membership a **precondition for reaching the control
plane at all**, so that a token is the second gate rather than the only one.
Concretely, three changes that must land in this order:

### 1. A `mesh` sentinel for `control_bind_host`

`control_bind_host` gains one new accepted value, `mesh`, alongside the literal
addresses it takes today. At node bring-up it resolves to the node's own mesh
address:

    tailscale ip -4        # the client is tailscale(1) under headscale too

and the resolved address, not the sentinel, is what reaches
`MAC_BIND_HOST`. Resolution is **fail-closed**: if the node is not on the mesh,
the service does not start with a wider bind than was asked for. Falling back
to `0.0.0.0` on a mesh that has not come up yet is precisely the failure this
is meant to prevent, and it would be invisible.

This is a sentinel rather than a new default. `0.0.0.0` stays the default for
the hub agent; a fleet opts in.

### 2. A configuration check that names the mismatch

`fleet_setup` validation gains a check with the same shape as the existing
`network.provider` one (`src/mac/fleet_setup.py:395`): when
`network.provider != none` and the hub's `control_bind_host` is `0.0.0.0`, and
when `hub_url`'s host does not fall inside the configured
`headscale.ip_prefix`, say so. A **warning, not a failure** — plenty of real
topologies legitimately front the hub with a proxy or a cluster service — but
stated, because today the mismatch is silent.

### 3. Host firewall rules as the second layer, separately

Binding to the mesh address stops the socket from accepting off-mesh
connections. It does not stop a local process, and it does not cover the
sidecar services (qdrant, firecrawl, webdav) that have their own bind
addresses. Per-platform firewall rules are the belt to the bind's braces, and
they are a distinct change with distinct rollback characteristics — `nft` on
Linux and `pf` on macOS share no implementation.

## Why this is not implemented in the change that carries this ADR

Every other part of this task was a wiring gap: something the system already
computed but failed to hand to the next component. This one changes what the
fleet is reachable from, and its failure mode is asymmetric in a way the others
are not.

If the vault publication breaks, a deploy warns and the key still travels the
old path. If `mac admin secret get` breaks, a command fails and prints why. If
the bind is wrong, **the hub becomes unreachable by the API you would use to
fix it** — every remaining repair is an ssh to each host, which is exactly the
situation `docs/break-glass-host-recovery.md` exists for. Shipping that in the
same change as two credential-plumbing fixes would mean the review that clears
the plumbing also clears the outage risk.

It also needs things this change cannot supply on its own:

- A **staged rollout**: one node, then the hub, with a verified route between
  each step, rather than a fleet-wide config flip.
- A **pre-flight**: prove the node's mesh address is up and the hub is
  reachable at it *before* narrowing the bind, not after.
- A **documented reversal**: the one-line edit and restart that puts a
  locked-out hub back on `0.0.0.0`, written down before it is needed.
- An answer for **non-mesh consumers**: the observability dashboard, any
  operator browser, and the k8s manifest (`deploy/k8s/mac-api/deployment.yaml`
  sets `MAC_BIND_HOST` on its own terms and does not go through
  fleet-node-install at all).

Filing it as its own task with its own review is the point, not a deferral of
work — the operator's third ask is a security-model change and should be
reviewed as one.

## Consequences

- Mesh membership becomes a real gate rather than a description of how traffic
  happens to flow. A stolen worker token is worth much less off-mesh.
- Enrollment becomes load-bearing: a node that cannot join the mesh cannot
  reach the hub at all. That raises the stakes on the key-provisioning path
  this change just finished — which is the correct order to build them in, and
  the reason this ADR follows rather than precedes it.
- Anything that reached the hub over a LAN address stops working at cutover.
  That set has to be enumerated per fleet before the flip, not discovered
  after.

## Alternatives considered

**Leave the bind at `0.0.0.0` and rely on tokens.** This is today. It is not
unreasonable — the scope model and `mac-853j` are real — but it makes the
credential the only boundary, and a credential is the thing most likely to
leak. The operator asked for defense in depth; this provides one layer.

**Enforce in application middleware: reject requests whose source address is
outside the mesh prefix.** Rejected as the primary mechanism. It accepts the
connection before refusing it, so the surface is still exposed to anything that
can reach the port, and a source address is a weak thing to authorize on when a
proxy sits anywhere in the path. It is a reasonable *additional* check once the
bind is narrow, and a poor substitute for one.

**Make `mesh` the default whenever `provider != none`.** Rejected for now. It
would silently narrow the bind for every existing headscale and tailscale
fleet on the next deploy, which is the fleet-wide outage described above with
no opt-in step. A sentinel first, a default later once fleets have run on it.

**Tie the bind to `hub_url` instead of adding a sentinel.** Rejected: `hub_url`
is what *clients* dial, which is legitimately a proxy, a DNS name, or a
published service address. Deriving the listen address from it conflates two
things that are allowed to differ.
