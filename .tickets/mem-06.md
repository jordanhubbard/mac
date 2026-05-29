---
id: mem-06
status: open
deps: []
links: [mem-07, mem-08, mem-09]
created: 2026-05-29T02:21:09Z
type: feature
priority: 2
assignee:
mac-task-id: task_5aa816eefa834b1580b462300fdafdf5
audit: memory-tier-2026-05-28
---
# Define vector memory tier schema: Qdrant collections, embedding model, TTLs

**Symptom.** `mac-qdrant.service` runs on rocky (`100.125.137.89:6333`), but `GET /collections` returns `[]`. `vector_refs` table in `mac.db` has **0 rows**. The medium-term and long-term memory tiers described in the design exist only as table stubs.

## Design

Define the contract before anyone writes code:

- **Collections**: how many, named what? Proposal: `mac_task_summary` (one point per closed task), `mac_evidence_digest` (one per evidence record with `summary` field), `mac_conversation_thread` (one per summarized thread). Open question: per-tenant collections vs shared with payload filter.
- **Embedding model**: which model, what dimension? Where does the embedding come from — TokenHub-routed or local sentence-transformers?
- **Payload schema**: what queryable fields (task_id, project, agent_id, evidence_type, created_at, tenant_id, tier=medium|long)?
- **TTL / retention per tier**: medium-term = 30 or 90 days? lifetime = forever or N years?
- **Promotion rule**: what moves a `memory_records` row from medium to long-term? (the nap consolidator in mem-08 needs this answer)

## Acceptance Criteria

- ADR-style doc in `docs/` defining collections, schema, model, TTLs.
- `deploy/install-qdrant-service.sh` provisions the named collections at install time (idempotent).
- Schema lands in `mac/models.py` as typed payloads so writers (mem-07) and readers (mem-09) share types.

Blocks: mem-07, mem-08, mem-09.
