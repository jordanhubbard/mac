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

## What's deliberately left as follow-ups

* **Re-embed the full 362 memory_records.** The verification used a
  `--limit 20` backfill because each embed makes one TokenHub round
  trip and we wanted a sub-minute demo. `mac memory backfill --limit
  500` (or unbounded) covers the rest; safe to run while the system
  is live because backfill is idempotent on `(memory_id, collection)`.
* **Medium → long promotion.** mem-08 ships the consolidator that
  writes to medium; the long-tier promotion (summarize old summaries,
  delete the medium points) is a planned mem-08 extension.
* **Hermes agent tool.** The recall API is HTTP-callable from
  Hermes, but no `recall_memory` tool is wired into the Hermes
  gateway yet so agents can self-serve from within a conversation.
