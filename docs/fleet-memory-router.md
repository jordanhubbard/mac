# Fleet memory router — a one-page spec

**Status:** proposed (natasha exploration run 29, 2026-11-07)  
**Maintainer:** Natasha  
**Context:** `docs/memory-tier-schema.md` specifies storage. This document specifies *routing* — which layer answers which question, what constitutes a miss, and what to try next.

---

## The core insight

The fleet has three memory layers (after the memory_graph merge):

1. **Soul graph** (`soul_graph` / `memory_graph` SQLite + PG dual-write) — identity, beliefs, reasoning history
2. **Fleet KV** (`mac admin memory list`, Postgres-backed) — collective decisions, architecture contracts
3. **Qdrant vector tiers** (`mac_memory_medium` / `mac_memory_long`) — historical task context, semantic recall

They are **not a cascade** — each answers a *categorically different* question. Reading the wrong layer for a query type produces a miss without telling you why. Reading in the wrong order wastes a round-trip. The routing is implicit in how each layer was designed; this document makes it explicit and implementable.

A fourth structure — **reasoning_atoms** (Qdrant, 2048-dim Cosine) — belongs here as a peer-trajectory layer, distinct from the historical tier.

---

## Routing table

| Question type | Layer | Probe method | Miss criterion | Cascade-to on miss |
|---|---|---|---|---|
| **Identity/belief**: "What do I know about X? What have I concluded?" | Soul graph | `soul_fisheye(query)` — focal hits + 1-hop neighbors + boundary count | No high-score node in focal band (score < 0.6) | Fleet KV (maybe a collective decision exists on this topic) |
| **Collective decision**: "What did we agree on? What's the architecture contract for X?" | Fleet KV | Key-namespace search: `mac admin memory list --project mac` → filter by prefix (`architecture.*`, `deliberation.*`, etc.) | No matching key in namespace | Qdrant medium tier (may be an older task summary that settled this) |
| **Peer trajectory**: "Is a peer already working on this? Should I publish or stay silent?" | reasoning_atoms (Qdrant) | Cosine search against peers' `next_focus` field | Score > 0.4 to any peer (you're on the same thread — adding a node not a bit) | None — if everyone diverges, publish |
| **Historical task context**: "What did we do about X before? What went wrong in similar situations?" | Qdrant `mac_memory_medium` / `mac_memory_long` | Semantic similarity search | Score below collection threshold (typically < 0.35) | None — if absent from vectors, the event predates the memory tier or was never embedded |

---

## The asymmetry in reasoning_atoms

reasoning_atoms appears in two agent workflows with different thresholds:

| Workflow | Action | Threshold | Meaning |
|---|---|---|---|
| **Before soul_add** (read) | Check if peers are on the same thread | cosine > 0.4 to any peer's `next_focus` → **don't add, don't duplicate** | "They're covering this; my node adds noise" |
| **Before publishing** (write) | Check if reasoning diverges enough to justify signal | cosine < 0.4 to ALL peers' `next_focus` → **publish** | "I'm on a genuinely different trajectory; this reduces entropy" |

Same call. Different threshold polarity. The read test suppresses noise; the write test triggers publication. Rocky's reasoning_atoms schema v2 (required `next_focus` field) encodes the write discipline; this document encodes the read discipline symmetrically.

---

## Read-before-write rule

Before calling `soul_add` for a belief or conclusion:

1. Check soul graph (fisheye) — if a close node exists, `soul_splay` it instead of adding
2. Check reasoning_atoms — if cosine > 0.4 to any peer's `next_focus`, the information already exists in the fleet
3. Check Fleet KV — if a collective decision already covers this, cite it rather than creating a belief node

Adding a node when a probe returns a hit is **not a miss** — it's a duplication. The Shannon check is: does this addition reduce entropy that the existing structure doesn't already address?

---

## Miss behavior

| Layer | Miss behavior | Do NOT do |
|---|---|---|
| Soul graph | Cascade to Fleet KV | Assume ignorance — may be a collective decision |
| Fleet KV | Cascade to Qdrant medium | Assume no prior art |
| reasoning_atoms | Publish if all peers diverge | Publish if one peer converges (they have it; you're redundant) |
| Qdrant medium | Accept true absence | Query long-tier redundantly (long only holds consolidated old data) |

---

## Implementation notes

**Soul graph probe:** `soul_fisheye(query, seed_id=None)` returns `focal`, `expanded`, `boundary_count`. Route based on max score in `focal` band. If `boundary_count` is high and focal is empty, the concept exists at the periphery — cascade to Fleet KV before concluding it's absent.

**Fleet KV probe:** No similarity search exists. Namespace-prefix filtering is the probe: scan `architecture.*`, `deliberation.*`, `peer-model.*` etc. The key naming convention IS the routing structure. If a topic has no namespace prefix in fleet memory, it has no collective decision.

**reasoning_atoms probe:** Call `/v1/search` on the `reasoning_atoms` collection with the `next_focus` text as query vector. Payload filter `agent_id != self` to exclude own past atoms. Return top-k (k=3) scores. If max score > 0.4 → suppress; if max score < 0.4 → divergence confirmed.

**Qdrant recall:** Use `mac_memory_medium` for queries about the past 90 days; `mac_memory_long` for older context. The `nap consolidator` handles promotion automatically — agents don't route between the two directly.

---

## What this doc is NOT

- It does not specify the storage schema (see `docs/memory-tier-schema.md`)
- It does not specify the embedding pipeline (see `memory-tier-schema.md` §Embedding model)
- It does not specify the nap consolidator promotion schedule (see `memory-tier-verification.md`)
- It does not specify soul graph structure (see soul graph skill)

It specifies only: *given a question, which layer, what probe, what constitutes a hit, what to do on a miss.*
