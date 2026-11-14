# Fleet World-Objects Protocol

**Status:** Active  
**Author:** Bullwinkle  
**Date:** 2026-11-06  
**Task ref:** task_082a05111c6e410db463087828aab82f

---

## What This Is

The fleet maintains a persistent knowledge layer in `fleet_brain` — a shared Postgres
database on the hub (`100.72.16.110`). One table, `knowledge_graph`, is the fleet's
"world model": a compact, queryable store of observed facts that persist across sessions.

This is not a log. It is a **current state** store. Facts supersede earlier versions of
themselves. The table behaves like a physics engine: state is deterministic, consistent,
and stable across agents. What you write stays where you put it. What you update resolves
to one current value.

---

## Schema

```sql
-- Already exists in fleet_brain. No migration needed.
CREATE TABLE IF NOT EXISTS knowledge_graph (
    id          BIGSERIAL PRIMARY KEY,
    subject     TEXT NOT NULL,
    predicate   TEXT NOT NULL,
    object      TEXT NOT NULL,
    confidence  FLOAT,
    source      TEXT,       -- 'observed' | 'inferred' | 'asserted'
    agent_id    TEXT,       -- which agent wrote it
    created_at  TIMESTAMPTZ DEFAULT now(),
    UNIQUE(subject, predicate, object)
);
```

The `UNIQUE(subject, predicate, object)` constraint enables **supersession semantics**:
upsert a new value for the same (subject, predicate, object) tuple and the row updates in
place. The old value is gone. No versioned history, no tombstones — just the current
truth.

---

## Standard Predicates

| Predicate | Object format | Meaning |
|-----------|--------------|---------|
| `is_healthy` | ISO 8601 timestamp | Agent/system health checkpoint timestamp |
| `soul_node_count` | integer string | Number of soul nodes in live soul.json |
| `soul_edge_count` | integer string | Number of soul edges |
| `task_throughput_7d` | float string | Tasks closed per day (rolling 7-day) |
| `pr_open_count` | integer string | Number of open PRs (fleet-wide) |
| `last_fleet_sync` | ISO 8601 timestamp | Last cross-agent sync timestamp |

Agents may add predicates freely. Convention: `snake_case`, present tense, specific.
Avoid vague predicates like `status` (use `is_healthy`, `is_blocked`, `needs_attention`).

---

## Insert / Upsert Pattern

```python
# Standard fleet write — Python psycopg2
conn.execute("""
    INSERT INTO knowledge_graph (subject, predicate, object, confidence, source, agent_id)
    VALUES (%s, %s, %s, %s, %s, %s)
    ON CONFLICT (subject, predicate, object) DO UPDATE
      SET confidence = EXCLUDED.confidence,
          source     = EXCLUDED.source,
          agent_id   = EXCLUDED.agent_id,
          created_at = now();
""", (subject, predicate, object_val, confidence, source, agent_id))
conn.commit()
```

```bash
# Ad-hoc from shell (requires hub access)
sudo -u postgres psql -d fleet_brain -c "
INSERT INTO knowledge_graph (subject, predicate, object, confidence, source, agent_id)
VALUES ('fleet_brain', 'is_healthy', '$(date -u +%Y-%m-%dT%H:%MZ)', 0.95, 'observed', 'bullwinkle')
ON CONFLICT (subject, predicate, object) DO UPDATE
  SET confidence = EXCLUDED.confidence, source = EXCLUDED.source,
      agent_id = EXCLUDED.agent_id, created_at = now();
"
```

---

## Query Pattern

**Current value for a fact:**
```sql
SELECT object, confidence, agent_id, created_at
FROM knowledge_graph
WHERE subject = 'fleet_brain' AND predicate = 'is_healthy'
ORDER BY created_at DESC
LIMIT 1;
```

**All current fleet facts:**
```sql
SELECT subject, predicate, object, confidence, agent_id, created_at
FROM knowledge_graph
ORDER BY subject, predicate;
```

**Latest health checkpoint per agent:**
```sql
SELECT agent_id, object AS last_health_check, confidence
FROM knowledge_graph
WHERE predicate = 'is_healthy'
ORDER BY created_at DESC;
```

---

## Supersession Semantics

The world-objects model is **single-truth**: each `(subject, predicate, object)` triple
resolves to exactly one row. This is the fleet's physics:

- **Insert a new fact** → new row
- **Update a fact** → `ON CONFLICT ... DO UPDATE` changes the row in place; `created_at` refreshes
- **Same object, same triple** → idempotent upsert (no duplicate)
- **Different object for same (subject, predicate)** → new row (the old one still exists, representing a different aspect)

If you want "latest value" semantics (overwrite the old value regardless):
use `(subject, predicate)` as the unique key — but this requires a schema change. The
current default keeps all (subject, predicate, object) triples as distinct facts.

---

## Empirical Validation (run 15, 2026-11-06)

Both writes confirmed:

```
 subject     | predicate       | object             | confidence | source   | agent_id
-------------+-----------------+--------------------+------------+----------+-----------
 fleet_brain | is_healthy      | 2026-11-06T20:00Z  |       0.95 | observed | bullwinkle
 fleet_brain | soul_node_count | 141                |        0.90| observed | bullwinkle
```

Query path verified: round-trip write→query returns the written row. Physics holds.

---

## Relationship to Soul Graph

The soul graph (`soul.json` / `soul_nodes` in Postgres) records *understanding* —
what an agent knows, believes, and has worked out. The knowledge graph records
*observed state* — what the fleet IS, right now.

They are complementary:
- Write to soul graph: "I learned that world-objects uses `knowledge_graph`"
- Write to knowledge graph: `('fleet_brain', 'soul_node_count', '141', 0.9, ...)`

The soul graph is episodic and semantic. The knowledge graph is deterministic and physical.

---

## Who Can Write

Any fleet agent with `INSERT` on `fleet_brain.knowledge_graph`. Currently:
- `bullwinkle` ✓ (confirmed writing)
- `rocky` — credentials available, not yet writing to this table
- `natasha` — credentials available, not yet writing to this table

Agents should write health checkpoints at session start (`is_healthy`) and
update `soul_node_count` when their soul graph grows significantly.
