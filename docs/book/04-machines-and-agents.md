---
schema: mac.docs.chapter.v1
chapter: 4
title: Machines and Agents
audiences: [operator, integrator]
timeout_seconds: 60
---

# Machines and Agents

A machine describes a compute location. An agent is a process identity attached
to a machine with capabilities and resource declarations. Keeping these objects
separate lets one physical host run several roles and lets pod-style machines
appear and disappear without losing agent history.

Use stable explicit identifiers in automation. Human-friendly names can change;
task leases and evidence always refer to canonical IDs.

```bash
mac --db "$DOCS_DB" init
mac --db "$DOCS_DB" machine register tutorial-host \
  --machine-id machine_tutorial \
  --labels '{"os":"linux","arch":"amd64","role":"worker"}'
mac --db "$DOCS_DB" agent register machine_tutorial tutorial-worker \
  --agent-id agent_tutorial \
  --instance-kind static \
  --capabilities python,docs \
  --resources '{"cpu":2,"memory_mb":4096}'
mac --db "$DOCS_DB" agent list
```

`instance_kind` is a first-class lifecycle property:

- `static` (the backward-compatible default) identifies a named installation
  whose machine trust is durable, such as a hub or named workstation agent.
- `fungible` identifies replaceable compute. A headless worker created through
  a provider API or a helper such as `hgx create` belongs here; its loss is a
  capacity event, not the loss of a static fleet identity.

Operators can set the classification at registration or change it explicitly:

```bash
mac --db "$DOCS_DB" agent register machine_tutorial headless-worker \
  --agent-id agent_headless --instance-kind fungible
mac --db "$DOCS_DB" agent update agent_headless --instance-kind static
```

Fungibility does not weaken trust checks. For example, `hgx list` establishes
provider inventory, while a successful `hgx ssh <session>` proves that a listed
session is actually reachable. A replacement host key must still be
re-attested through the provider before its strict SSH pin is changed.

Fungibility is also distinct from the legacy `resources.ephemeral` TTL flag. A
fungible agent may retain a durable MAC identity across replacement compute;
an ephemeral agent identity is tombstoned after its heartbeat TTL expires.

Production agents authenticate with their own revocable worker credentials.
They must never copy the hub administrator token. A heartbeat advertises current
availability, while a lease grants authority over one task attempt for a bounded
time. Stale processes cannot reuse an expired lease to write evidence.

Fleet deployment generates and verifies the runtime identity projected onto
each agent. Registration is necessary, but it is not proof that the node can
clone repositories, invoke its coding CLI, or pass the sandbox contract; those
are onboarding acceptance checks covered later.
