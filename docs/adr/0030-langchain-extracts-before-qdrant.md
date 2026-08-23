# ADR 0030: LangChain extracts meaning on the agent; hub Qdrant only stores the extract

- Status: Proposed
- Date: 2026-08-23
- Decision owner: MAC fleet owner
- Related: [ADR 0002](0002-memory-store-at-scale.md) — Qdrant is the production
  vector store; this ADR does not reopen that choice
- Related: [ADR 0007](0007-hermes-boundary-mood-nap-soul-memory.md) — nap and
  soul memory already hand off to `VectorWriterService`; the handoff stays,
  the *payload* changes
- Related: [ADR 0017](0017-token-spend-is-metered-at-the-router.md) — extraction
  is a metered model call on the agent, not an unmetered hub side-effect
- Numbering: **0029** is claimed by the in-flight route-ladder ADR on #634

## Context

Qdrant on the hub is the right destination. The suspicion, recorded 2026-08-23
as `task_b5351642`, is that we are not giving it good data.

`VectorWriterService.embed_memory` takes `record.content` and embeds it. The
payload builder will JSON-parse that content when it can, copy a few known
keys (`mac.dream.v1`, `mac.deployment_learning.v1`), and otherwise set
`summary` to the first 2,000 characters of whatever the writer had. Transcripts
are split on a 4,000-character window so the embedding backend does not
truncate; that is a length cap, not a meaning extract.

The writers that feed this path today are hub-side and agent-adjacent, not
extractors:

| Call site | What it currently embeds |
| --- | --- |
| `VectorWriterService.embed_memory` / `embed_backlog` | raw `memory_records.content` |
| `memory_promotion.py` | the same content, re-embedded into a warmer tier |
| `nap_consolidator.py` | nap summaries, then `embed_memory` |
| `dreaming/store.py`, `dream_log_import.py` | dream records (already structured) and imported logs |
| `macong memory embed` / control-plane remember | whatever the operator or agent posted |
| transcript chunker (`TRANSCRIPT_COLLECTION`) | raw dialogue windows |

#580 / `task_cf393901` repaired the pipe: mixed embedding spaces, ingestion
outage, model/dim provenance. That work is complementary. A reliable pipe that
carries undifferentiated text still stores undifferentiated text.

Recall then searches those vectors. Near-neighbours of a transcript window or a
raw task body are not near-neighbours of a claim. The collection looks full
and answers confidently; the answers are the wrong objects.

## Decision

### 1. Qdrant stays the fleet store

One hub Qdrant, shared across that hub's agents, remains the destination.
No second vector store. No agent-local ANN index that later has to be
reconciled. ADR 0002 still holds.

### 2. LangChain on the agent is the arbiter of meaning

Before a record is published to hub Qdrant, the agent that produced the data
runs a LangChain extraction step locally: chunk by discourse (not by character
count), summarise, type, and cite. The hub embeds *that extract* and upserts
it. The hub does not run LangChain over raw payloads "to be helpful".

LangChain is required at the publish edge, not on the hub process. An agent
without it does not silently fall back to embedding the raw dump. No extract,
no upsert. The ledger row may still exist; the vector must not.

### 3. Only an extract record is a legal Qdrant point

The published unit is a versioned extract, not `memory_records.content`.
Minimum shape (`mac.memory_extract.v1`):

- `text` — the meaning-bearing string that will be embedded
- `kind` — closed set (`claim`, `decision`, `procedure`, `outcome`,
  `preference`, `incident`, `citation_only`, …)
- `citations` — pointers back to the source (memory id, evidence id, transcript
  span, file/line, URL). An extract without a citation is a rumour.
- `source_schema` / `source_id` — what was extracted
- `extractor` — `{library: "langchain", chain, model}` so a bad extract is
  attributable
- the existing multi-tenant payload keys from ADR 0002 (`tenant_id`,
  `agent_id`, `project`, `tier`, `record_type`)

`VectorWriterService` rejects an upsert whose payload is not this schema, or
whose `text` is a raw dump (no `kind`, no `citations`). That rejection is the
test the task named: a raw dump without an extract record does not become a
point.

Records that are *already* extracts may pass without a second LLM call.
`mac.dream.v1`, `mac.deployment_learning.v1`, and `mac.fleet_learning.v1` qualify
when they already carry a summary, a kind, and citations. Character-chunked
transcripts do not.

### 4. The writers change; the store does not

Implementation (not this document) walks the call sites in the table above
and inserts the extract step *before* `embed_memory`. The writer keeps
`ensure_collection / upsert / query / delete`. It gains a schema check.
It loses the right to embed freeform content.

Backfill of existing points is a later, bounded job. This ADR does not require
re-embedding history before the contract is adopted for new writes.

## Consequences

- Recall quality becomes a property of the extract, not of whoever happened
  to call `remember`. That is the point.
- Publish costs a model call per extract. Meter it at the agent router
  (ADR 0017). Do not hide it inside the hub upsert.
- Agents that cannot run LangChain stop contributing vectors. That is
  fail-closed on *quality*, and fail-soft on *availability*: ledger writes
  and task progress continue; semantic recall is thinner until the agent
  can extract.
- Tests must cover the rejection, not only the happy path: posting
  `record.content = "<transcript>"` and calling `embed_memory` is a failure.
- Prompt text and skills that say "write this to Qdrant" have to say
  "extract, then publish".

## Alternatives considered

**Keep embedding raw content and hope a better embedding model is enough.**
Rejected. A better embedding of a 4,000-character transcript window is still
an embedding of a transcript window. #580 already moved the embedding model;
recall did not become meaning-aware.

**Run LangChain on the hub as a Qdrant write interceptor.** Rejected. The hub
does not have the agent's working set, the files, or the conversation that
made the record. Extraction without that context is a second guess. It also
puts unmetered model spend on the control plane.

**Agent-local vector stores, sync to hub later.** Rejected. That is a second
store and a reconciliation problem. The destination is not in doubt.

**Make extraction optional, embed raw as a fallback.** Rejected. Optional
extraction is the current behaviour with extra steps. The collection would
again mix claims with dumps, and recall cannot tell them apart.

## Non-goals

- Replacing Qdrant, or adding a second ANN backend.
- Choosing a specific LangChain chain, chunker, or chat model. Those are
  implementation; this ADR fixes the contract: extract on the agent, store
  the extract on the hub.
- Re-embedding the existing collection. New writes first.
- Changing the hash-stub embedding backend used in tests. The stub embeds
  extract `text`, not raw dumps.
