# NemoClaw runtime context — AGENTS.md injection guidance.
#
# This file documents the runtime context injection model for NemoClaw pilot
# deployments and explains how to populate the MAC runtime context file that
# is injected into every Hermes agent session.
#
# Hermes reads MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN at startup and injects
# its contents as an operator instruction block into every session prompt.
# The fleet deploy is responsible for writing this file before starting the
# gateway service.
#
# This is NOT the injected context itself — it is guidance for operators.
# The actual runtime context file lives at:
#   <HERMES_HOME>/mac-runtime-context.md
# and is rendered by the MAC fleet deploy from the agent's mac.env.
#
# See deploy/hermes/mac-runtime-context-prompt.patch for the Hermes-side
# implementation that reads and injects this file.

# NemoClaw pilot — AGENTS.md context injection guidance

## Overview

The NemoClaw pilot runs a Hermes gateway inside an OpenClaw sandbox on a
single host, alongside the existing hermes gateway service.  MAC injects
runtime context into every agent session via the MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN
mechanism introduced in the mac-runtime-context-prompt.patch.

The injected context file (`mac-runtime-context.md`) must be present at
`HERMES_HOME` before the gateway starts.  If `MAC_HERMES_RUNTIME_CONTEXT_REQUIRED=1`
is set (default for NemoClaw), the gateway refuses to start without it.

## What to put in mac-runtime-context.md

The context file is Markdown.  Fleet deploy renders it from the agent's
identity and task ledger config.  A minimal NemoClaw pilot context looks like:

```markdown
## NemoClaw Pilot Context

- Agent: <agent-id>
- Fleet: <fleet-name>
- Role: nemoclaw-gateway
- Hub: http://hub.example.internal:8789
- Pilot Slack workspace: <workspace-name>
- MAC task ledger: http://hub.example.internal:8789 (mac task CLI)
```

## AGENTS.md vs mac-runtime-context.md

| Source              | Scope              | Mechanism                     |
|---------------------|--------------------|-------------------------------|
| AGENTS.md (repo)    | All agents reading this repo | Hermes cwd/git-root scan |
| mac-runtime-context.md | This agent only | MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN env var |

For the NemoClaw pilot, AGENTS.md in the repository supplies project-level
instructions such as task ledger CLI and fleet host resolution.
The per-agent mac-runtime-context.md supplies this instance's identity,
hub URL, and operator-specific runtime instructions.

## Environment variables that control injection

| Variable | Default | Purpose |
|---|---|---|
| MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN | `$HERMES_HOME/mac-runtime-context.md` | Path to the context file |
| MAC_HERMES_RUNTIME_CONTEXT_REQUIRED | `1` (NemoClaw) | Refuse to start if file is absent |

## Coexistence with the existing hermes gateway

NemoClaw runs on port 18765 with `HERMES_HOME` set to a pilot-specific
directory (`~/.hermes-nemoclaw` by default).  This isolates its config,
slack_accounts.json, and runtime context from the existing hermes gateway
service.  The existing service is NOT modified or restarted by the NemoClaw
pilot deploy.
