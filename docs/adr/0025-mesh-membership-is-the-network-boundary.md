# ADR 0025 - Mesh membership is the network boundary, and the vault is how you get in

- Status: **Partially accepted** — the enrollment-key half is implemented; the
  binding half is proposed and deliberately not yet built (see *Status of each
  decision*)
- Date: 2026-08-20
- Decision owner: MAC fleet owner
- Related: ADR 0013 (the hub allocator is authoritative), ADR 0019 (privilege
  is an ACL on a resource tree), ADR 0022 (a gate returns a named decision)

## Context

A fleet that should not ride on anyone's personal Tailscale tailnet — a demo
fleet, a customer fleet, an air-gapped one — runs its own coordination server.
`deploy/install-headscale.sh` already does the hard part: it installs
headscale, writes `server_url`, starts it under the node's supervisor, waits
for `/health`, creates a user, and generates a reusable one-year pre-auth key.

Three things were missing around it, and they are different in kind.

**The key had exactly one distribution channel: SSH.** The generated key was
written to `HEADSCALE_PREAUTHKEY` in the hub's env file and nowhere else. Every
worker that joined got it because the deploy pipeline forwarded that env var
over SSH during that worker's own bootstrap. Nothing that the pipeline does not
SSH into could obtain it — an operator's laptop, a CI runner, the provisioner
host itself. The observable cost: the first QUICKDEMO fleet gave up and pasted
a *personal* Tailscale auth key into `~/.mac/.env`, creating a
`tailscale-auth-<fleet>` secret as a workaround, which is the exact coupling to
a personal tailnet the self-hosted control plane exists to avoid.

**No CLI could reveal a secret.** `SecretsService` is a real vault, but its
access flow is `request_secret()` → `SecretHandle` → `reveal_secret()`, and
`reveal_secret` matches `accessor_agent_id` against a registered `Agent` row on
a trusted `Machine`. The CLI's `secret` subtree had `set/list/delete/rotate/
access/audits` and no reveal at all. Even for a registered agent there was no
terminal path to a value; for a machine that is not a fleet agent the handle
flow is not merely missing, it is *circular* — it would require fleet-agent
registration in order to fetch the enrollment key that lets you join the fleet.

**Nothing ties API traffic to the mesh.** The hub control plane binds
`0.0.0.0` by default (`deploy/deploy-mac-fleet.sh:1352`,
`src/mac/fleet_setup.py:528`, and the hub entry in `deploy/fleet/config.yaml`),
and `hub_url` is whatever was configured at setup time — a mesh IP, a cluster
DNS name, or loopback — with no cross-check against `network.provider`. A2A
delegation (`POST /a2a`) is scope-gated like every other route but is not
network-confined. So "all agent-to-agent traffic goes over the mesh" is at
present a description of how the fleet happens to be addressed, not a property
anything enforces.

## Decision

### 1. The vault is the distribution channel for the enrollment key; the env file is a cache

`install-headscale.sh` keeps writing `HEADSCALE_PREAUTHKEY` to the hub env file
— the deploy pipeline reads it there and that path is fine — but the key's
authoritative home is a `SecretRecord` named `headscale-preauthkey-<fleet>`,
scoped by capability (`deploy`, `mesh-join`) rather than by agent id, because
the set of nodes that may join is open-ended by definition.

Publication is a **separate step** from generation, in
`deploy/headscale-key-vault.sh`, because the two have different preconditions:
generation needs headscale, publication needs the hub to answer, and on a fresh
fleet the network layer is installed *before* the control plane. So the first
publish attempt is expected to be deferred, and the outcome is stamped into the
env file (`HEADSCALE_PREAUTHKEY_VAULT=published|deferred`) so that "the hub was
not up yet" never looks like success. Re-running is idempotent: an existing
secret of that name is rotated in place, keeping its id, scopes and audit
trail, which is what a regenerated key actually is.

### 2. Reveal-by-name is the non-agent path, and the `secret` token scope is its credential

`POST /secrets/{name}/resolve` already existed for in-fleet consumers (the
Slack and forge fetchers). It is the right mechanism for this too, and a second
reveal endpoint would have been a mistake. What it needed was a CLI
(`mac admin secret get <name> [--raw]`) and an explicit answer to "who may
call it".

That answer is the **`secret` token scope**, which a hub token may carry
without being bound to any agent. It is a real credential with real limits, not
a bypass: tenant isolation (closed on this route as part of this work — it was
the one `/secrets/*` route that skipped `_assert_secret_tenant`), the existing
per-principal rate limit, an audit row per call naming the principal and the
declared purpose, and a 404 rather than an empty string for a
missing-or-disabled secret.

The handle flow remains preferred wherever the caller *is* a registered agent:
it is single-use and time-limited, and reveal-by-name is neither.

### 3. The mesh address is derived from the provider, not hand-configured (proposed)

`control_bind_host` is already a per-agent config field; it simply never
consults `network.provider`. The proposal is that when `network.provider` is
`tailscale` or `headscale`, the hub's bind address is **derived** from the
node's mesh interface (`tailscale ip -4`, within the fleet's `ip_prefix`,
default `100.64.0.0/10`) rather than defaulting to `0.0.0.0`, with:

- an explicit escape hatch (`control_bind_host` set literally in the fleet
  config always wins, including back to `0.0.0.0`);
- a named decision at deploy time, per ADR 0022 — `mesh-bound`,
  `explicitly-unbound`, `no-provider`, or `mesh-address-unavailable` — recorded
  in the deploy manifest, so an operator can see which one happened rather than
  inferring it from a port scan;
- a **fail-closed refusal** when the provider is a mesh and the mesh address
  cannot be determined, because silently falling back to `0.0.0.0` in that case
  would turn an enforcement mechanism into a coin flip;
- a `hub_url` / provider cross-check that warns when `hub_url` names an address
  outside the mesh prefix while the provider is a mesh, since that combination
  means agents are reaching the hub off-mesh no matter what the bind says.

Precedent for derivation over configuration already exists in
`canonicalize_mesh_ssh_target(target, provider=network_provider)`: SSH targets
are already normalized against the provider, and the control-plane address is
the same question asked about a different port.

### 4. Binding is the enforcement layer; firewall rules are an addition, not the mechanism

Bind-address enforcement is chosen over `iptables`/`pf` rules as the primary
mechanism because it is portable across the platforms mac deploys to (linux,
darwin, wsl2), needs no root at runtime, cannot drift out of sync with the
service it protects, and fails in the safe direction: a socket that was never
opened on a public interface cannot be reached by a rule that was never
applied. Packet filtering may be layered on for defence in depth, but a fleet
whose only mesh guarantee is a firewall rule has its guarantee in a different
subsystem from its listener.

### 5. Authentication is not replaced by network confinement

Mesh binding narrows *reachability*; it does not authorize anything. Token
scopes, tenant isolation, and `mac-853j`'s refusal to fail open on a
non-loopback bind all continue to apply unchanged. A node on the mesh is still
an unauthenticated stranger to the control plane until it presents a token.
This is worth stating because "it's on the private network" is the oldest way
an authorization gate quietly stops being enforced.

## Status of each decision

| # | Decision | Status |
|---|----------|--------|
| 1 | Key lands in the vault, publication separate and idempotent | **Implemented** |
| 2 | `mac admin secret get` + `secret` scope as the operator credential | **Implemented** |
| 3 | Mesh-derived `control_bind_host` | **Proposed — not built** |
| 4 | Binding over firewall as the mechanism | **Proposed — not built** |
| 5 | Auth still applies | Already true; stated so it is not eroded |

Decisions 3 and 4 change the network security model and can, if wrong, make a
hub unreachable from the operator's own machine — the failure mode is
"locked out of the fleet", and it lands on whoever is least able to recover it.
They are recorded here so the enrollment-key work has a stated boundary rather
than an implied one, and are left for a change that can be reviewed on its own
terms with a rollback path.

## Consequences

- A control node that the deploy pipeline never touches can join the fleet's
  mesh: fetch the key from the vault, `tailscale up --login-server`, done. That
  is the case that previously forced a personal auth key into the fleet.
- The hub's own generated key becomes rotatable and auditable like any other
  credential, instead of living only in a file on one host.
- `mac admin secret get` puts plaintext on stdout by design. That is the point
  of the command, and the audit row records that it happened — but it means the
  `secret` scope should be minted deliberately and not folded into a
  general-purpose operator token out of convenience.
- Until decision 3 lands, "A2A traffic goes over the mesh" remains a
  description of the addressing, not an enforced property. Anything that claims
  otherwise is claiming more than the code does.

## Alternatives considered

**Have workers keep taking the key over SSH only, and skip the vault.** Rejected:
it works for exactly the nodes the pipeline SSHes into, and the whole ask is
about the nodes it does not. It also leaves the key unrotatable and unaudited.

**Add a second, operator-specific reveal endpoint.** Rejected: `/secrets/{name}
/resolve` already is an audited, scope-gated, rate-limited reveal-by-name. A
parallel endpoint would have meant two authorization boundaries to keep in
agreement, and they would eventually disagree.

**Let the operator use the existing handle flow by registering their laptop as a
fleet agent.** Rejected as circular for the mesh case — registration requires
reaching the hub, which is what the mesh key is for — and wrong in general: a
person is not an agent, and giving an operator an Agent row to borrow its
authorization confuses identity with role (ADR 0019).

**Make publication to the vault fatal on failure.** Rejected: on a fresh fleet
the hub is not up when the network layer installs, so this would fail every
first deploy. Deferred-and-stamped, with `HEADSCALE_VAULT_REQUIRED=1` available
for fleets that want the strict behavior, keeps the failure visible without
making the ordering a landmine.

**Bind the hub to the mesh address now, as part of this change.** Rejected: see
*Status of each decision*. The blast radius is an unreachable hub, and it does
not share a review with a key-distribution change.
