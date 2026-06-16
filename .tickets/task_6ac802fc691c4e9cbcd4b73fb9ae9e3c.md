---
id: task_6ac802fc691c4e9cbcd4b73fb9ae9e3c
status: open
deps: []
links: []
created: 2026-06-16T07:16:11.123640+00:00
type: task
priority: 1
mac-task-id: task_6ac802fc691c4e9cbcd4b73fb9ae9e3c
---
# EPIC: memory tier for 50–200 agents per hub (ADR 0002)

Refiled from stale mirror `.tickets/mem-store-01.md` during .tickets cleanup.

# EPIC: memory tier for 50–200 agents per hub (ADR 0002)

Spec: `docs/adr/0002-memory-store-at-scale.md`. Qdrant stays the production
default (not overkill at the target scale); the work is the abstraction + the
gaps around it.

## Work breakdown (by leverage)

- [x] **mem-store-02: real embeddings on by default.** Default a hub to a real
      embedding model (batched, async, fixed model+dim per collection); hash
      stub becomes offline/test-only. Biggest recall-quality win, store-agnostic.
- [ ] **mem-store-03: `VectorStore` interface.** `ensure_collection/upsert/
      query/delete` ABC; move Qdrant specifics behind it; `MAC_VECTOR_BACKEND`
      = qdrant (prod) | sqlite-vec/lancedb (dev/single-agent). Test seam.
- [ ] **mem-store-04: hard multi-tenant scoping.** Every point payload carries
      tenant/agent/project/tier/record_type/salience/created_at; recall filters
      by tenant + agent|project; explicit fleet/project-shared layer ([[dream-06]]).
- [ ] **mem-store-05: bounded growth.** Wire [[dream-04]] decay per hub +
      scalar/product quantization so the index stays bounded as agents/history
      grow.
- [ ] **mem-store-06: fail-soft recall.** Bounded timeout; Qdrant-unavailable →
      empty result, never hang the agent (same lesson as [[loop-01]]); health
      check + resource caps + monitoring on the Qdrant dependency.
