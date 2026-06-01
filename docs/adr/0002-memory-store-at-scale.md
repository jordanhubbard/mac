# ADR 0002 — Memory store / vector tier at fleet scale (50–200 agents per hub)

- Status: **Proposed**
- Date: 2026-05-31
- Decision owner: Jordan Hubbard
- Context: the fleet today is 3 agents (the minimum), but the target is
  **50–200 agents per user, each user starting with their own hub**. The
  question "is Qdrant the best vector store for our purposes" has a different
  answer at 3 agents than at 200. This ADR specs the memory tier for the
  *target* scale, not the current minimum.

## TL;DR verdict

**At the target scale, Qdrant is the right default — not overkill.** The earlier
"a separate vector service is a heavy moving part" concern holds for a 1–3 agent
toy, but 50–200 agents per hub, each generating conversation/nap/deployment
memories over time, is a **millions-of-vectors-per-hub** workload. That is
exactly what a dedicated ANN store (HNSW + payload filtering + quantization) is
for; brute-force SQLite would not keep up.

The real work is **not** "pick a different store." It is the five things around
the store that are currently missing or stubbed, plus an abstraction so the
store is a config choice rather than a rewrite.

| Concern | Today | At 50–200 agents |
| --- | --- | --- |
| Embeddings | **hash stub by default** (`MAC_MEMORY_EMBED_BACKEND=hash`) | real, batched, fixed model/dim per collection — **the dominant lever** |
| Scoping | thin (subject_type/subject_id) | hard multi-tenant: every vector carries `{tenant, agent_id, project, tier, record_type}`; recall filters on them |
| Growth | unbounded (observability bloat already seen) | salience + decay/forget ([[dream-04]]) + scalar/product quantization or it dies |
| Store binding | `VectorWriterService` *is* the Qdrant client (`_VECTOR_DB_LABEL="qdrant"`) | a `VectorStore` interface; Qdrant prod, embedded (sqlite-vec/LanceDB) for dev/single-agent |
| Failure mode | `recall_memory` hard-requires Qdrant; can hang | **fail-soft**: timeout → empty result, never hang the agent (same lesson as [[loop-01]]) |

## Decision

1. **Keep Qdrant as the default production backend.** One Qdrant per hub,
   shared across that hub's agents (the existing hub-owns-the-tier model). It
   scales to the target via HNSW + payload filters + quantization on a single
   node; shard-by-tenant or cluster only if a single hub ever exceeds one node.
2. **Introduce a `VectorStore` interface** (`ensure_collection / upsert / query /
   delete`) and move the Qdrant specifics behind it. `MAC_VECTOR_BACKEND`
   selects `qdrant` (default) or an embedded `sqlite-vec`/`lancedb` impl for
   dev, CI, and single-agent installs. This is the same "make the backend a
   connector" instinct as the ticketing connector — the store stops being a
   rewrite and becomes a config choice + a test seam.
3. **Make embeddings real and batched.** Default a hub to a real embedding model
   (via the router — see [[th-merge-01]]/TokenHub), fixed model+dim per
   collection, batched + async on the write path. The hash stub stays only as
   an offline/test fallback. *This is the single biggest recall-quality win and
   is independent of the store.*
4. **Hard multi-tenant scoping.** Every point's payload carries
   `tenant_id, agent_id, project, tier, record_type, salience, created_at`.
   Recall always filters by tenant + (agent or project) so agent A never recalls
   agent B's private memory, while the explicit fleet/project-shared layer
   (deployment_learning, [[dream-06]]) is queryable across agents by design.
5. **Bound growth with salience + decay.** [[dream-04]] decay runs per hub on a
   schedule; promote high-salience to long, demote/forget stale low-salience,
   and enable quantization so the resident index stays bounded as agent count
   and history grow.
6. **Fail-soft recall.** Recall gets a bounded timeout and returns empty on
   Qdrant unavailability rather than raising/hanging — a wedged vector store
   must degrade gracefully, exactly like the agent LLM-call bound in [[loop-01]]
   after the TokenHub wedge.

## Consequences

- Qdrant stays; the work is the abstraction + the five gaps, tracked in
  **mem-store-01**.
- Small/dev/single-agent installs get an embedded backend with no separate
  service — the "fewer moving parts" benefit is preserved where it actually
  applies (not at production scale).
- Operationally, Qdrant becomes a monitored, resource-capped, health-checked
  dependency with a fail-soft client — it must not be able to wedge the fleet
  the way TokenHub just did.

## Non-goals

- A vector-DB bake-off. Qdrant is decided for production; the interface keeps
  the door open without blocking on a comparison.
- Re-embedding history eagerly — backfill is a bounded background job.
