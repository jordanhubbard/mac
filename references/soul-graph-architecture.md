# Soul graph architecture

The soul graph stores an agent's long-term memories and the relationships
between them. It is distinct from shared operational state: soul nodes explain
what an agent remembers, while fleet world objects describe facts that agents
currently assert about external subjects.

## Shared world objects

Shared world facts use the existing `knowledge_graph` table. Agents identify a
fact by `(subject, predicate, object)` and attach confidence, source, and agent
metadata. The standard predicates are `is_healthy`, `is_running`,
`has_version`, `is_blocked_by`, and `has_state`.

Writers supersede metadata for an identical fact with an upsert:

```sql
INSERT INTO knowledge_graph
    (subject, predicate, object, confidence, source, agent_id)
VALUES
    ('fleet_brain', 'is_healthy', '2026-11-06T20:00Z', 0.95,
     'observed', 'observer-agent')
ON CONFLICT (subject, predicate, object) DO UPDATE SET
    confidence = EXCLUDED.confidence,
    source = EXCLUDED.source,
    agent_id = EXCLUDED.agent_id;
```

This is deliberately a current-state model: the update discards the previous
confidence and provenance. It does not replace soul-graph memory or provide a
historical ledger. See [the fleet world-objects protocol](../docs/fleet-world-objects-protocol.md)
for vocabulary, replacement semantics, and query examples.
