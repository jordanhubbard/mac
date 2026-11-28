# Fleet world-objects protocol

Fleet agents use the existing `knowledge_graph` table to publish persistent
facts about the world. A fact is the tuple `(subject, predicate, object)` with
confidence, source, and asserting agent metadata. This convention does not
introduce another world-state table.

## Standard predicates

Use these predicates consistently:

| Predicate | Meaning | Typical object |
|---|---|---|
| `is_healthy` | The subject's observed health | Observation time or health value |
| `is_running` | Whether a process or service is running | Boolean or observation time |
| `has_version` | The subject's deployed version | Version or commit identifier |
| `is_blocked_by` | A dependency currently blocking the subject | Blocking object identifier |
| `has_state` | The subject's current lifecycle or operational state | Stable state name |

Subjects and objects should use stable fleet identifiers rather than local
hostnames or paths. Confidence is a number from `0` through `1`, and `source`
describes how the assertion was obtained, such as `observed` or `reported`.

## Current-state supersession

`knowledge_graph` has a unique constraint on `(subject, predicate, object)`.
Writers use `INSERT ... ON CONFLICT DO UPDATE` so a repeated assertion updates
the existing fact rather than creating a duplicate:

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

This protocol represents current knowledge, not an audit log. An upsert loses
the previous confidence and provenance values. Systems that require history
must record it separately.

The conflict key includes `object`. To replace a fact whose object changes,
such as a new `has_version` value, remove the superseded tuple and insert the
new tuple in one transaction. Otherwise both objects remain valid assertions.

## Querying facts

A querying agent can retrieve the latest health assertion with:

```sql
SELECT object, confidence
FROM knowledge_graph
WHERE subject = 'fleet_brain'
  AND predicate = 'is_healthy'
ORDER BY created_at DESC
LIMIT 1;
```

The timestamp-valued object in this example identifies when health was
observed. `created_at` determines which matching assertion was stored most
recently when the deployed schema provides that metadata column.
