---
id: fleet-01
status: open
deps: []
links: [mem-08, dream-06, conn-01, loop-01]
created: 2026-05-31T00:00:00Z
type: feature
priority: 1
audit: group-consciousness-review
discovered_via: architecture_review
---
# EPIC: give hermes chat sessions a fleet "group view" + shared consciousness

## Problem

Historically hermes sessions were "autistic" — a chat session knew nothing
about the larger world of agents (no group view, no shared context). Merging
hermes-agent into mac in-process + the memory-tier upgrade was meant to fix
this. **Status: the merge built the foundations but the chat path is NOT yet
wired to use them.**

## What the merge already gives (verified)

- **Self task-awareness (YES)** — each agent's OWN tasks/workspace are injected
  into its system prompt via `~/.hermes/mac-runtime-context.md`
  (`_load_mac_runtime_context`, prompt_builder.py:1383; assembled by
  `hermes_runtime.write_runtime_context`). A session knows *its own* work.
- **Shared project memory EXISTS (PARTIAL)** — `deployment_learning` records are
  `subject_type="project"` (fleet-shared), and the executor recalls them
  (task_executor.recall_deployment_lessons). The nap consolidator promotes them
  into the vector tier. But this is only used by the *executor*, not chat.
- hermes runs **in-process** with mac → it can call `cp.list_tasks/list_agents/
  observations/recall_memory` directly (no HTTP), and shares the DB + memory.

## What's still missing (the actual gap)

- **(b) Fleet/group view (NO)** — a chat session cannot see other agents' tasks,
  leases, status, or in-flight work. An identity boundary even tells it to stay
  silent for other agents. No fleet/roster tool or context.
- **(c) Shared memory in chat (NO)** — the Slack chat session recalls only its
  own `hermes_instance` personality memory, never the project/fleet memory the
  executor uses. No "group consciousness" in conversation.
- **Home-channel work reporting (worker-only)** — only the MacWorker posts work
  status to home channels; the chat session can't report real fleet WIP.

## Plan (now easy because hermes is in-process with mac)

- [ ] **fleet-02: inject a fleet snapshot into the runtime context.** Extend
      `write_runtime_context` to include a compact, current view of the fleet
      (agents + status + their active tasks + recent completions), refreshed
      live (not just at deploy). The chat agent then *sees* the group.
- [x] **fleet-03: a `fleet` tool (DONE).** `src/mac/_hermes/tools/fleet_tool.py`
      (added to `_HERMES_CORE_TOOLS`): `fleet status` = roster + who's working on
      what; `fleet message <agent>` = send an agentbus message to another agent;
      `fleet inbox` = read messages addressed to me. Calls the hub HTTP API with
      the agent's env token; gated on hub access. Tests: test_fleet_tool.py (6).
      This delivers the core 'know what others are doing + talk over agentbus'.
- [ ] **fleet-04: shared-memory recall in chat.** Have the chat turn recall
      project/fleet memory (like the executor) and inject a "what the fleet has
      learned / is working on" block — true shared consciousness, building on
      [[dream-06]] (fleet-level consolidation).
- [ ] **fleet-05: chat-driven home-channel digests.** Let the gateway post
      periodic fleet WIP/standup digests to the home channel with real task
      data (the agents' chatty channel becomes a real group awareness surface).
