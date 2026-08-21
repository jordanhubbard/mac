# Memory tier — end-to-end verification

Verification record for the audit-driven memory-tier work shipped in
mem-06 through mem-10 + mem-04 + mem-02 + mem-11 + mem-12 + mem-13.
This document captures the operational evidence that the system works
end to end on a live fleet, not just in unit tests.

**Date:** 2026-05-30
**Hub:** hub (`<host>`, Tailscale `<mesh-ip>`)
**Commits exercised:**
`106abce` (mem-06 schema) →
`898085e` (mem-07 writer) →
`4a5bc90` (mem-08 consolidator) →
`1aa1c9c` (mem-09 recall) →
`eed129f` (mem-10 health) →
`ef3baba` (TokenHub embedding backend)

**Test counts:** 631 unit tests green throughout, 1 unrelated e2e
deselected.

> Historical topology note: this record correctly describes the TokenHub-backed
> fleet exercised on 2026-05-30. Standalone TokenHub has since been retired from
> the default deployment; the current vector writer uses the same
> OpenAI-compatible interface through `OPENAI_BASE_URL`, normally MAC's in-mac
> router. Do not use the commands below as current TokenHub deployment guidance.

## What was verified

| Component | How | Result |
|---|---|---|
| **Qdrant provisioning** | `WORKSPACE=… bash deploy/install-qdrant-service.sh` with `MAC_MEMORY_EMBEDDING_DIM=2048` | Both `mac_memory_medium` and `mac_memory_long` collections created at the right dim with HNSW indexes. |
| **Embedding via TokenHub** | `MAC_MEMORY_EMBED_BACKEND=tokenhub MAC_MEMORY_EMBED_MODEL=nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2 mac admin memory backfill --limit 20` | 20/20 of the hub's real `memory_records` embedded into Qdrant with no failures. Vectors are 2048-dim from the real NVIDIA embedding model, not the hash stub. |
| **Semantic recall (the actual test)** | `mac admin memory recall "github repository for the ACC project"` (paraphrased — no exact words match the stored content) | Top hit is the `repo-beads-acc` ACC project record at cosine score **0.4115**, with the next four hits all ACC-related task records at 0.36–0.37. The model successfully matched "github repository for ACC" → stored JSON about `jordanhubbard/ACC` without word-level overlap. |
| **Round-trip recall** (write → embed → search → retrieve) | `tests/test_vector_writer_service.py::test_embed_memory_round_trip_recall_finds_the_record` | Three memories written, embedded, queried by one memory's content → that memory ranks #1 with score > 0.99 in fake Qdrant; in unit form for CI. |
| **Consolidator + recall** | `tests/test_nap_consolidator.py::test_consolidate_and_recall_end_to_end` | Two agents author distinct memory_records, consolidator produces one nap_summary per agent, both summaries embed into Qdrant, recall against one summary's content returns that summary as top hit. |
| **Structured dream artifacts** | `tests/test_nap_consolidator.py::test_consolidate_writes_structured_dream_artifact_with_evidence` and `::test_dream_artifacts_embed_with_payload_filters_and_recall_rules` | Nap consolidation writes typed `mac.dream.v1` records with evidence/scope/confidence/retrieval metadata; vector payload filters can recall only matching dream artifacts by project, agent, scope, kind, and confidence. |
| **Real consolidator on the hub** | `mac admin nap consolidate agent_hub` | agent_hub's 31 real memory_records on the hub were consolidated into per-task summaries, each summary embedded into the medium tier; recall against one summary's content returned it at score 1.0. |
| **Health check** | `mac admin memory health` against the hub | Schema `mac.memory_health.v1`. `memory_records_count: 362`, `vector_refs_count: 20`, `qdrant.collections.mac_memory_medium.points_count: 20` — all three numbers consistent. `observability_events_count: 545,297` (down from the audit's 2,088,341 thanks to mem-02 prune + mem-04 suppression). Alerts surface the `no_nap_history` warning correctly because no `nap_runs` row exists yet (consolidator was driven via CLI, not the nap lifecycle). |
| **Defense-in-depth invariants** | unit tests for mem-11/12/13 | `operator_result` for repo-coupled tasks rejected at write (mem-11). Reviews capped at 3 retracts per task → fail (mem-12). `git ls-remote` verifies `pushed=true` claims (mem-13). |
| **CLI parity** | `mac admin memory backfill / recall / health / embed` and `mac admin nap consolidate` | All work in both local (`--db`) and hub modes; remote dispatch wraps the HTTP routes. |

## What this proves

* The wiring from `memory_records` → consolidator → vector_writer →
  Qdrant → recall round-trips correctly on real data.
* The embed backend is pluggable: hash for tests/offline, TokenHub
  for production semantic recall — and TokenHub is the path the
  fleet already uses (`OPENAI_API_KEY` + `OPENAI_BASE_URL`
  shipped pre-set in `~/.mac/mac.env`).
* Semantic recall is real: a paraphrased query that shares **no exact
  words** with the stored content still returns the right memory as
  the top hit.
* The disk-bloat failure modes from the 2026-05-28 audit are gone:
  observability table shrunk by ~75% (2.09M → 545K rows), runaway
  review loops are bounded at 3 retracts, beads-bridge spam is
  silenced when the bridge is off.
* The invariant gaps that caused the original `task_d7c51a0b`
  incident are all closed.

## Autonomy (added 2026-05-30)

The consolidator is now wired into a systemd timer that ticks every 15
minutes, queries `mac admin nap due` for agents whose window has opened, and
runs `mac admin nap cycle <agent_id>` for each:

| Step | Detail |
|---|---|
| `deploy/systemd/mac-nap-tick.{service,timer}` | Oneshot service + 15-min OnUnitActiveSec timer. Service body: `mac admin nap due | python -c (...extract agent_ids...) | xargs mac admin nap cycle`. |
| `deploy/install-nap-tick-service.sh` | Installer mirroring `install-observability-prune.sh` (detects User= from mac.service, substitutes into template). Provisions `/etc/mac/nap-tick.env` with commented-out TokenHub embedding knobs. |
| Verified on the hub | Cleared `agent_hub`'s `last_completed_at`; ran `systemctl start mac-nap-tick.service`; service picked up agent_hub, drove the full cycle (begin → consolidate → embed → complete), produced `nap_run=nap_c00be4c…` with `status=completed`, agent back to IDLE. No operator command in the chain. |

What this means: between `mac admin memory backfill` (which embeds historic
memory_records once) and the nap-tick timer (which catches newly
authored ones on each agent's daily window), the memory tier maintains
itself going forward.

## 2026-08-21 audit — three failures the health snapshot could not see

A hand audit of the live instance found that ingestion had been dead for
27 days and nothing had said so:

```
mac_memory_medium   points 667   newest embedded_at 2026-07-25T20:16:47Z
mac_memory_long     points   0   never written
  inside mac_memory_medium:
    nvcf/nvidia/llama-3.2-nv-embedqa-1b-v2   601 points
    azure/openai/text-embedding-3-large       66 points
```

Newest `created_at` and newest `embedded_at` were one second apart, so the
pipeline was keeping up right until it stopped — an abrupt halt during the
Hermes → OpenClaw migration window, not a backlog.

The reason a month passed before anyone noticed is that `memory_health`
asked Qdrant exactly one question per tier: `points_count`. Points persist,
so a collection frozen since July still reports 667 healthy-looking points.
`mac.memory_tier_probe` now also reads the `embedded_at` and
`embedding_model` payload every point already carries
(`mac.models.MacVectorPayload`), which makes all three findings alertable:

| Alert | Severity | Fires when |
|---|---|---|
| `stalled_vector_ingestion` | critical | A collection has points but its newest `embedded_at` is older than `--vector-ingestion-max-age-hours` (default 24h). This is the 27-day gap. |
| `unwritten_memory_tier` | critical | A declared tier holds zero points while a sibling tier is populated. This is `mac_memory_long`: nothing in the tree promotes medium → long (see the follow-up below, open since 2026-05-30), so the collection advertises a capability the fleet does not have. |
| `mixed_embedding_spaces` | critical | One collection holds vectors from more than one embedding model. Vectors from different models are not comparable, so similarity search silently mixes two spaces and returns wrong neighbours with no error anywhere. Resolve by re-embedding to one model or splitting per model. |
| `vector_ingestion_age_unknown` | warning | The collection is larger than `MAC_MEMORY_HEALTH_SCAN_LIMIT` (default 20,000). Qdrant's scroll is id-ordered, not `embedded_at`-ordered, so a truncated scan has not necessarily seen the newest point; the probe says so rather than inventing a stall. Raise the limit or add an `embedded_at` payload index. |

Run it with `mac admin memory health`, or `GET /v1/memory/health`.

### Probes must agree with the thing they probe

Qdrant binds to the fleet's Tailscale address, not loopback, so an on-node
`curl http://127.0.0.1:6333/collections` returns nothing while the service is
healthy. A health check written against localhost reports an outage that is
not happening.

The same shape of bug was live in `hermes_startup._qdrant_endpoint_from_env`,
which read `QDRANT_URL`, `QDRANT_ADDRESS`, and `QDRANT_FLEET_URL` but not
`MAC_QDRANT_URL` — the name that *leads* the canonical cascade in
`mac.memory_config.QDRANT_URL_ENV_NAMES` and the one the hub and vector
writer resolve through first. A fleet configured only that way was reported
`missing_endpoint` while memory worked fine. The probe now resolves through
the shared cascade, so it cannot drift from the resolver again.

Still open, deliberately not fixed here: `mac.cli._build_vector_writer`
falls back to `http://127.0.0.1:6333` when no Qdrant env var is set. That is
the loopback assumption in writer form — on a fleet node it points the writer
at nothing. It is left alone because changing it is a behaviour change for
local development that wants its own decision.

### The long tier now has a writer

`mac_memory_long` held zero points because nothing anywhere passed
`tier="long"` to the vector writer. `mac.memory_promotion` is that writer.

A medium-tier `vector_refs` row that has sat for `min_age_days` (default 30,
`MAC_MEMORY_PROMOTION_MIN_AGE_DAYS`) without a re-embed is *settled*, and
settled memories are promoted into `mac_memory_long` through the normal write
path — same deterministic point id, so promotion is idempotent and re-running
it costs nothing. Selection reads the ledger rather than Qdrant, because
asking the store you are trying to fill what belongs in it is circular.

It runs inside the nap cycle rather than on a timer of its own. That is a
direct lesson from finding 1: the thing that killed ingestion on 2026-07-25
was a *separate* scheduled job that nobody migrated. Promotion rides the
schedule that already exists and already holds a vector writer. Bounded at
`MAC_MEMORY_PROMOTION_MAX_PER_PASS` (default 50) so one nap does not hold an
agent in DRAINING for a whole backlog sweep, and any failure is captured into
the cycle's `promotion_error` rather than raised.

Retiring the medium copy is opt-in (`mac admin memory promote --drop-medium`,
`drop_medium=true` on the route). Copy-then-verify is the default while the
tier is new, and a point is only ever dropped after its long-tier write
succeeded. Also available standalone:

```bash
mac admin memory promote --dry-run          # what would move
mac admin memory promote --min-age-days 60  # or POST /v1/memory/promote
```

Deliberately not included: re-summarizing promoted records into denser
long-tier artifacts. That needs a summarizer and a quality bar; putting an
unevaluated LLM step on the critical path of "make the tier real" would trade
one unmeasured behaviour for another.

### Reconciling the two embedding spaces

Recall used to search the whole collection, so a query embedded by one model
was scored against points embedded by another. Same dimension, different
space: the score is a dot product between vectors that share nothing but a
length. `VectorWriterService.recall` now filters on the `embedding_model`
payload by default, which turns a wrong answer into a smaller one — a loss
the operator can see rather than a wrongness they cannot. Pass
`strict_embedding_space=False` to look across spaces deliberately.

That contains the damage; it does not remove it. To collapse a collection
back onto one model:

```bash
mac admin memory reconcile-embeddings --report-only   # which models, how many
mac admin memory reconcile-embeddings --dry-run
mac admin memory reconcile-embeddings                 # re-embed the minority
```

Each mismatched point is re-embedded from its `memory_records` row and
replaces itself in place. The `vector_refs` row is re-stamped with the new
model at the same time: the table is UNIQUE on
`(vector_db, collection, point_id)` and point ids are deterministic, so
without an update the ledger would keep naming a model that is no longer in
the collection — the provenance lie the audit had to untangle by hand.

Points whose source memory is gone cannot be rebuilt; they are reported as
`orphaned` rather than skipped silently, because deleting them is the
operator's call.

## What's deliberately left as follow-ups

* **Re-embed the full 362 memory_records.** The verification used a
  `--limit 20` backfill because each embed makes one TokenHub round
  trip and we wanted a sub-minute demo. `mac memory backfill --limit
  500` (or unbounded) covers the rest; safe to run while the system
  is live because backfill is idempotent on `(memory_id, collection)`.
* **Medium → long promotion.** ~~Planned mem-08 extension.~~ **Built
  2026-08-21** — see "The long tier now has a writer" above. What
  remains a follow-up is the *summarizing* half of the original design
  (condense old summaries rather than copy them), which needs a
  summarizer and a quality bar of its own.
* **Hermes agent tool.** The recall API is HTTP-callable from
  Hermes, but no `recall_memory` tool is wired into the Hermes
  gateway yet so agents can self-serve from within a conversation.
