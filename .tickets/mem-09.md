---
id: mem-09
status: open
deps: [mem-07]
links: []
created: 2026-05-29T02:21:15Z
type: feature
priority: 3
assignee:
mac-task-id: task_9620b1d4622749a086d5595395d205f2
audit: memory-tier-2026-05-28
---
# Recall API: mac memory recall + hermes tool for vector tier read path

**Symptom.** Even after mem-07 lands and Qdrant fills with task/evidence/conversation embeddings, there is no read path. Agents and humans cannot ask "show me prior work similar to this task" or "what did we learn last time we worked on the auth subsystem".

## Acceptance Criteria

- New CLI: `mac memory recall <query>` with flags `--tier {medium,long,both}`, `--project`, `--limit`, `--min-score`.
- New API: `GET /v1/memory/recall?q=...&tier=...` returning ranked results with `{memory_id, task_id, score, summary, source_uri}`.
- Hermes-side: the gateway exposes `recall_memory` as a tool so agents can self-serve during a conversation (matches the `hermes-agent` pattern where memory is a single tool).
- Results include provenance: clicking through a `memory_id` lands on the originating task/evidence in the dashboard.
- Test: insert known-good memories, query with semantically-similar wording, assert ranking matches expectation.

Blocked by mem-07.
