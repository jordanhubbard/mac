---
schema: mac.docs.chapter.v1
chapter: 16
title: APIs, AgentBus, and Integrations
audiences: [integrator, contributor]
timeout_seconds: 90
---

# APIs, AgentBus, and Integrations

The CLI and Fleet IDE use the same control-plane API available to integrations.
OpenAPI describes request and response contracts. Scoped bearer credentials
authorize HTTP operations; idempotency keys protect retryable task creation.

AgentBus is durable ordered content transport between agents. It does not grant
execution authority, bypass leases, or mark a task complete.

```bash
mac --db "$DOCS_DB" admin init
mac --db "$DOCS_DB" admin machine register bus-host --machine-id machine_bus
mac --db "$DOCS_DB" agent register machine_bus sender \
  --agent-id agent_sender --capabilities docs
mac --db "$DOCS_DB" agent register machine_bus receiver \
  --agent-id agent_receiver --capabilities docs
mac --db "$DOCS_DB" admin agentbus open agent_sender \
  --recipient-agent-id agent_receiver --topic tutorial \
  --stream-id stream_tutorial
mac --db "$DOCS_DB" admin agentbus append stream_tutorial agent_sender \
  --payload '{"message":"hello"}' --payload-encoding json --final
mac --db "$DOCS_DB" admin agentbus read stream_tutorial agent_receiver >/dev/null
mac --db "$DOCS_DB" admin agentbus close stream_tutorial agent_sender
mac admin integrations --help >/dev/null
```

Notifiers translate durable events into operator-visible messages. GitHub and
other repository integrations generate work or publication actions, but MAC's
task ledger remains canonical. Integration observations record authority and
failure classification without storing credentials.
