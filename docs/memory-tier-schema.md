# MAC vector memory tier — schema, collections, model, TTLs

**Status:** proposed (mem-06). This document defines the contract the
vector writer (mem-07), nap consolidator (mem-08), recall API (mem-09),
and health check (mem-10) build against. Amend by PR; the schema lives
in code in `src/mac/models.py`.

**Audit context:** the 2026-05-28 review of hosta's `mac.db` (3.1GB,
2.09M observability events) found that the Qdrant container was running
on `100.64.1.1:6333` with **zero collections** and `vector_refs` had
**zero rows**. The medium-term and long-term memory tiers documented in
the design existed only as table stubs; no writer, no reader, no schema
contract. This document closes that gap.

## Tiers

Three logical tiers, mapped to the storage backends already in the fleet:

| Tier | Storage | Latency target | Retention | What lands here |
|---|---|---|---|---|
| **Short-term** | SQLite `mac.db` (existing) | < 10ms reads | per-table prune (mem-02) | Live task ledger, evidence, conversation summaries, observability metrics |
| **Medium-term** | Qdrant collection `mac_memory_medium` | < 100ms similarity search | rolling 90 days (TTL via nap consolidator) | Closed task summaries, completed-evidence digests, conversation-thread summaries — anything an agent might semantically recall within ~quarter |
| **Long-term** | Qdrant collection `mac_memory_long` | < 200ms similarity search | indefinite, operator-managed | Distilled lessons (nap-summaries older than 1y), historic incidents, ratified architecture decisions |

The split between medium and long is **not** a hot-vs-cold cache. It's
**raw-vs-distilled**: medium holds the per-task / per-evidence atoms;
long holds the post-consolidation summaries that condense many medium
points into a single "what we learned" record. Both are vector-indexed.

## Collections

Two collections in Qdrant, not three:

* `mac_memory_medium`
* `mac_memory_long`

**Decided: one collection per tier, not per concept.** An earlier draft
proposed `mac_task_summary`, `mac_evidence_digest`, `mac_conversation_thread`
as separate collections. Rejected because:

* Hermes' typical recall query is "what did we do about X" — a single
  similarity search across all artifact types beats three queries with
  client-side merging.
* Per-collection HNSW indexes have a fixed memory cost; three small
  collections fit worse than two right-sized ones.
* The payload's `subject_type` field already partitions client-side for
  callers that want it.

**Decided: shared collections across tenants, partitioned by payload
filter.** No per-tenant collections. Tenancy boundary lives in the
payload (`tenant_id`) and is enforced at the recall API (mem-09), not
in the storage layout. Operators of single-tenant fleets pay no
overhead; operators of multi-tenant fleets get a single index to
maintain.

**Open question:** soft-deleted points. Today Qdrant's TTL purges
points wholesale. If we want a 30-day undo window after promotion to
long, we'd need a `deleted_at` payload field + a sweeper. Defer until
operationally needed.

## Payload schema

Stored on every vector point. Typed as `MacVectorPayload` in
`src/mac/models.py`:

```python
{
  "schema":          "mac.memory.v1",       # required, always this exact string
  "tier":            "medium" | "long",     # which collection this point belongs to
  "subject_type":    "task_summary" | "evidence_digest" | "conversation_thread" | "nap_summary" | "dream",
  "subject_id":      "task_xxx" | "ev_xxx" | "thread_xxx" | "mem_xxx",
  "memory_id":       "mem_xxx",             # FK to memory_records in SQLite
  "task_id":         "task_xxx" | None,     # set when the memory relates to a task
  "project":         "<project name>" | None,
  "agent_id":        "agent_xxx" | None,    # who produced the underlying artifact
  "tenant_id":       "tenant_xxx" | None,   # for multi-tenant filtering
  "evidence_type":   "<type>" | None,       # mirrored from evidence_validators when applicable
  "record_type":     "nap_summary" | "dream:<kind>" | "...",
  "dream_kind":      "decision_rule" | "failure_pattern" | "knowledge_snippet" | "tool_pattern" | "routing_signal" | None,
  "dream_scope":     "agent" | "project" | "fleet" | None,
  "dream_confidence": "low" | "medium" | "high" | None,
  "dream_confidence_score": 0.0-1.0 | None,
  "created_at":      "<ISO 8601>",          # original artifact time, NOT embedding time
  "embedded_at":     "<ISO 8601>",          # when this vector was written
  "embedding_model": "<model name + version>",
  "tags":            ["..."],               # short labels for filtering
  "summary":         "<= 2000 chars",       # human-readable text that was embedded
}
```

The `summary` is the recall-renderable text for the point. For freeform
memories it is the embedded source excerpt; for typed dream artifacts it
is the artifact's concise `summary` while the full JSON remains in
`memory_records.content`. Storing it on the payload (not just on
`memory_records` in SQLite) lets the recall API render results without a
SQLite round-trip per hit, at the cost of duplicating a small string.
Worth it.

**Decided:** `schema` is a literal string, not a versioned shape. When
we need v2, we'll add it as `mac.memory.v2` and the writer will dual-write
during migration. The recall API filters by `schema in (allowed)`.

## Embedding model

**Decided: TokenHub-routed via the Hermes gateway.** mac already runs
`mac-tokenhub.service` and `mac-hermes-gateway.service`. The vector
writer (mem-07) calls TokenHub for embeddings rather than spinning up
its own model server. This keeps the writer stateless and reuses the
existing key + quota plumbing.

**Decided model: `text-embedding-3-small` (1536 dims).** Rationale:
- Wide compatibility (TokenHub already routes it).
- Smallest of the modern OpenAI-class models — keeps point size and
  HNSW memory tight.
- "Small" beats "ada-002" on benchmarks at lower cost.
- Local alternative (`bge-small-en-v1.5`, 384 dims) is a one-liner swap
  for offline fleets; the model name lives in the payload so cross-model
  searches are filterable.

The model name + version on every payload is **load-bearing for
migrations**: when we swap models, the recall API can choose to query
the older points until the writer re-embeds them, instead of returning
stale-distance results.

**Open question:** dual-write during model swap. Probably yes (write
both old and new vectors for a window), defer the spec until we actually
swap.

## TTLs and promotion

| When | What happens |
|---|---|
| Within minutes of artifact creation | Vector writer (mem-07) embeds and lands the point in `mac_memory_medium` |
| Daily, per agent | Nap consolidator (mem-08) summarizes the agent's last 24h of memory_records into `nap_summary` rows and typed `mac.dream.v1` artifacts, then embeds both into medium tier |
| Medium-tier point reaches 90 days old | Nap consolidator re-summarizes nearby points, lands ONE long-tier vector, deletes the underlying medium points |
| Long-tier point reaches ~1y old | Reviewed by operator (no auto-action). Operators MAY re-summarize or merge. |

**Decided: TTL = 90 days for medium, indefinite for long.** Rationale
for 90 days: matches a fleet's typical "what happened last quarter"
recall window and gives the nap consolidator a clear promotion deadline.

**Decided: nap-consolidator-driven promotion, not time-driven.**
Qdrant's built-in TTL would purge medium points without summarizing —
information lost. The nap consolidator pulls the points, asks the LLM
to synthesize, writes the synthesis to long, and only then deletes the
medium points it summarized.

## Provisioning

`deploy/install-qdrant-service.sh` provisions the two collections
idempotently at install time. The vector dimensions and distance metric
are baked into the install script based on the decided model:

```yaml
mac_memory_medium:
  vectors: { size: 1536, distance: Cosine }
  hnsw_config: { m: 16, ef_construct: 100 }

mac_memory_long:
  vectors: { size: 1536, distance: Cosine }
  hnsw_config: { m: 16, ef_construct: 200 }   # tuned for retrieval quality, not insert speed
```

If `MAC_MEMORY_EMBEDDING_DIM` is set in the install env, it overrides
the 1536 default (lets offline fleets swap in a smaller local model).

## What this contract does NOT specify

* The vector writer's batching / retry policy (mem-07's concern).
* The nap consolidator's prompt for summarization (mem-08's concern).
* The recall API's query parser / ranking knobs (mem-09's concern).
* The dashboard tile for memory-health (mem-10's concern).
* Operator workflows for purging the long tier.

Each of those is its own ticket. This document only fixes the storage
shape so they can work against a stable target.
