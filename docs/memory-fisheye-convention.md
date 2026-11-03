# Memory fisheye convention

## Handoff

Fleet handoffs carry a durable task reference and retrieval parameters, not a
narrated summary that can drift:

```json
{"task_ref":"task_...","memory_query":"canonical publication failure","top_k":5,"expand_hops":1,"boundary_hop":2}
```

The receiver resolves the task record, retrieves the focal memories, expands
one graph hop, and sees a count for hop two before spending more context.

## Access tiers

Memory has three explicit scopes: `owner-private` (one agent), `group-scoped`
(an enumerated fleet/project group), and `fleet-wide` (all principals in the
hub). The database stores scope and owner/group identifiers and applies row
filtering. The MCP layer is the enforcement point because it has authenticated
caller identity and must construct every DB/vector query with that scope
predicate; the database additionally rejects malformed scope combinations.
Separate per-agent soul files remain owner-private by construction. Promotion
to group or fleet scope is an explicit write, never inferred from content.

## Retention

Retention is score weighted, not a record-count cap. Non-pinned items score as
`(access_count + 1) * exp(-seconds_since_access / 86400)`: retrieval refreshes
their access time, while non-use makes them fade. Pinned axioms score infinity
and cannot be pruned. A maintenance pass removes only items below the configured
score floor and reports candidates before applying deletion.

## Dream validation

The former hourly dream/synthesis feature is not active in this tree. Migration
`0004_drop_removed_feature_tables.sql` drops `dream_runs` and
`dream_candidate_entries`, and `docs/home-consolidation.md` records removal of
the pipeline. Archived field notes show historical candidates, but they are not
evidence of a currently producing cron. Therefore the useful-output validation
result is explicitly **not running**, rather than a false claim based on stale
rows. Reintroducing synthesis requires a separately owned scheduler, run ledger,
and a check that recent successful runs emitted accepted or actionable output.

## Write discipline

Consolidated memory stays atomic: one durable fact or lesson per node, linked to
its task/evidence instead of copied narration. Repeated facts update or link the
existing node. Identity axioms alone are pinned; transient logs and conversational
repetition are not promoted. `MEMORY.md` is an index, not a transcript.
