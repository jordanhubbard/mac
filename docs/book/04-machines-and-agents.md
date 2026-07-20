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
  --capabilities python,docs \
  --resources '{"cpu":2,"memory_mb":4096}'
mac --db "$DOCS_DB" agent list
```

Production agents authenticate with their own revocable worker credentials.
They must never copy the hub administrator token. A heartbeat advertises current
availability, while a lease grants authority over one task attempt for a bounded
time. Stale processes cannot reuse an expired lease to write evidence.

Fleet deployment generates and verifies the runtime identity projected onto
each agent. Registration is necessary, but it is not proof that the node can
clone repositories, invoke its coding CLI, or pass the sandbox contract; those
are onboarding acceptance checks covered later.
