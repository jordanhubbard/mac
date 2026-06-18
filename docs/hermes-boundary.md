# Hermes Boundary

`mac` is the durable control plane. Hermes is the human-facing agent runtime.
The system needs both roles.

Without Hermes, OpenClaw, or an equivalent runtime, the control plane can track
tasks and machines but has no conversational continuity, adaptive personality,
or lived memory. The hub, worker-1, worker-2, and similar agents are not just
worker names; they are Hermes identities with souls, user context, prior
conversation memory, and situational behavior.

## Responsibility Split

Hermes owns:

- Slack, Telegram, Discord, CLI, and other message application gateways.
- Conversation sessions, threading, turn context, and reply behavior.
- `SOUL.md`, `USER.md`, `MEMORY.md`, skills, and user-facing memory semantics.
- Personality adaptation from prior conversations and current circumstances.
- The decision to transform a user request into one or more durable tasks.

`mac` owns:

- Durable tasks, claims, leases, dependencies, state transitions, and history.
- Fleet machines, agents, capabilities, health, and dispatch.
- Structured agent messages that cannot carry arbitrary execution payloads.
- Evidence, reviews, publication records, and completion gates.
- Scoped secret handles, audit logs, runtime manifests, rollouts, and rescue.
- Operational provenance linking external conversations or issues to tasks.

The boundary is intentionally asymmetric. Hermes may know about `mac` tasks so
it can tell the user what happened. `mac` should not ingest Hermes personal
memory unless a Hermes instance deliberately writes a sanitized operational
summary.

## Durable Identity Model

`mac` records enough identity to route and audit work across many users and
instances:

- `tenant`: an isolated organization, household, or single-user deployment.
- `user`: a human inside a tenant.
- `persona`: the durable identity contract for a Hermes agent, including
  `soul_ref` and `memory_scope`.
- `hermes_instance`: a running or recoverable Hermes identity, such as `hub`.
- `platform_binding`: the Slack workspace/channel, Telegram chat, Discord
  guild/channel, or other message surface attached to a Hermes instance.

When Hermes creates a task, `mac` records an `origin` block in task metadata:

```json
{
  "type": "hermes_interaction",
  "tenant_id": "tenant_...",
  "user_id": "user_...",
  "hermes_instance_id": "hermes_...",
  "persona_id": "persona_...",
  "platform_binding_id": "binding_...",
  "conversation_ref": "slack://T123/C456/1712345678.000100"
}
```

It also records a `memory_boundary` block that says Hermes remains
authoritative for personality and user memory, while `mac` records operational
provenance only.

Phase 2 implements this through `mac.hermes_adapter` and the `mac-hermes` CLI.
Hermes gateway code should use that adapter, or an equivalent API client, rather
than importing `ControlPlane` or editing SQLite directly.

## Interaction Flow

1. A user talks to Hermes in Slack, Telegram, Discord, CLI, or another gateway.
2. Hermes interprets the request using its current session, `SOUL.md`, user
   memory, skills, and any relevant long-term context.
3. If durable work is needed, Hermes creates an interaction task in `mac`, or
   asks `mac` to start a workflow when the request is already a multi-step plan.
4. `mac` dispatches the task or workflow-created node task to a healthy capable
   worker agent.
5. Workers produce typed evidence. Reviewers approve or request changes through
   the same durable review API.
6. Publication or completion evidence closes the task. For Beads-backed work,
   `mac` also mirrors key milestones into Beads comments prefixed
   `mac-ledger v1` so the issue itself shows what happened.
7. Hermes reads the durable result and updates its own conversational/user
   memory when appropriate.

The adapter can prepare the completed-task memory write-back payload, but the
actual write belongs to Hermes because Hermes owns `MEMORY.md` and any
associated memory store.

## Non-Goals

`mac` should not:

- Reimplement Hermes gateways.
- Own `SOUL.md`, `USER.md`, or `MEMORY.md` semantics.
- Store raw private conversation memory as task metadata.
- Decide how a personality should speak to a user.
- Become a model provider router.

The clean-room goal is a small, inspectable operational substrate that can serve
many richer Hermes identities.
