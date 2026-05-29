---
id: mem-08
status: open
deps: [mem-06, mem-07]
links: []
created: 2026-05-29T02:21:13Z
type: feature
priority: 2
assignee:
mac-task-id: task_e97da0c99de041cf8dc9823f86ab8993
audit: memory-tier-2026-05-28
---
# Activate nap consolidator: begin_nap rolls up daily memory into summaries

**Symptom.** `mac nap` CLI is labelled *"daily memory consolidation"* (`src/mac/cli.py:1498`). `nap_schedules` has 0 rows on rocky. `nap_runs` has 0 rows. `AgentStateService.begin_nap` (`agent_state_service.py:314`) flips agent state DRAINING and `complete_nap` flips it back to IDLE — neither writes a memory, summary, or vector.

**Why it matters.** Without consolidation, the medium-term tier (mem-07) gets one point per raw `memory_record` and the long-term tier never receives anything. The whole "short → medium → lifetime" promotion model collapses into "short → medium dupes".

## Acceptance Criteria

- `begin_nap` triggers (or enqueues) a consolidation pass for the agent:
  1. Read since-last-nap: tasks the agent touched, evidence it produced, memory records it wrote.
  2. Summarize per task and per project (LLM-backed via Hermes gateway already in the fleet).
  3. Write the summary as a `memory_records` row with `record_type='nap_summary'`.
  4. Hand off to vector writer (mem-07) to embed into the `medium` tier.
- Periodic promotion: nap-summaries older than the medium TTL (per mem-06) get re-embedded into the `long` collection with their `medium` point deleted.
- A nap schedule is auto-created for every registered agent (deterministic offset per `agent.name`, already implemented).
- A systemd timer (or in-process scheduler) calls `mac nap next` and invokes `begin_nap` when the window opens.
- After one nap cycle on rocky: `nap_runs` > 0, `memory_records` count grows, `vector_refs` count grows.

Blocked by mem-06, mem-07.
