---
id: mem-07
status: open
deps: [mem-06]
links: [mem-08, mem-09, mem-10]
created: 2026-05-29T02:21:11Z
type: feature
priority: 2
assignee:
mac-task-id: task_bce9643d87f84ebe8f37661fc364bc42
audit: memory-tier-2026-05-28
---
# Build vector writer: memory_records to vector_refs to Qdrant

**Symptom.** `MemoryService.record_vector_ref()` exists in `src/mac/memory_service.py:217` and the `vector_refs` table is migrated, but **no code in `src/mac/` calls it**. The provenance bridge between `memory_records` (SQLite) and Qdrant points is purely theoretical.

## Acceptance Criteria

- A writer service (proposed: `src/mac/vector_writer_service.py`) that:
  - Reads new `memory_records` rows (or accepts an explicit `embed(memory_id)` call from the nap consolidator in mem-08).
  - Calls the embedding model (per mem-06 decision).
  - Upserts the point into the appropriate Qdrant collection.
  - Records the resulting `(vector_db, collection, point_id, embedding_model)` via `record_vector_ref`.
- Backfill mode: one-shot pass over existing `memory_records` (331 rows on rocky) to populate Qdrant.
- Failure handling: embed/upsert failures don't crash the writer; failed rows get a retry record in a small outbox table (similar pattern to `task_transition_outbox`).
- Test: write a memory record, run the writer, assert `vector_refs` row exists and Qdrant `GET /collections/{c}/points/{id}` returns the payload.

Blocked by mem-06. Blocks mem-09, mem-10.
