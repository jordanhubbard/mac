# memory_graph

SQLite-backed graph memory library for fleet agents. Stdlib only — no pip dependencies.

## Setup (per agent)

```python
import sys
sys.path.insert(0, '/path/to/mac/lib/memory_graph')
import memory_graph as mg
mg.init_db('/path/to/your/.mac/data/<name>-memory-graph/memory_graph.db')
```

## API

- `add_node(content, kind, tags, pinned, visibility, auto_link)` → node_id
- `add_edge(from_id, to_id, relation)` — typed relation
- `access(node_id)` — reinforces score (call when you use a node)
- `fisheye(node_id, hops=1)` → focal + hop1 summaries + hop2 counts
- `search(query)` → keyword search ranked by score
- `top(n=8)` → highest-score nodes (session preamble)
- `decay_all()` → exponential decay, pinned/axiom immune

## Node kinds
`memory`, `axiom` (always pinned), `skill-ref`, `task-ref`

## Visibility tiers (via tags)
- `visibility:private` — agent-only (soul, secrets)
- `visibility:personal` — default
- `visibility:fleet` — shareable to other agents via Qdrant (future)

## Per-agent setup

Each agent creates their own DB:
```bash
mkdir -p ~/.mac/data/<name>-memory-graph
```

Then set MEMORY.md to:
```
Your memory lives in a graph. First tool call every session:
  import sys; sys.path.insert(0, '/path/to/lib/memory_graph')
  import memory_graph as mg; mg.init_db('~/.mac/data/<name>-memory-graph/memory_graph.db')
  for n in mg.top(8): print(f"[{n['kind']}] {n['summary']}")
Never write to this file again. The graph is the store.
```

## Gardener (daily maintenance)

```bash
python3 lib/memory_graph/gardener.py --db ~/.mac/data/<name>-memory-graph/memory_graph.db
```

Runs: decay, archive low-score, flag isolated nodes, detect near-dupes, surface promotion candidates, check skill-ref health.

Schedule with Hermes cron: `every day at 3am`.

## Edge types (VALID_RELATIONS)

`add_edge(from_id, to_id, relation)` accepts these relation types:

| Relation | Meaning |
|---|---|
| `related-to` | loose association |
| `cites` | one node references another as a source |
| `refines` | one node sharpens or narrows another |
| `corroborates` | one node supports/confirms another |
| `derived-from` | one node was produced from another |
| `contradicts` | one node conflicts with another |

**Note — soul graph vocabulary gap:** The soul graph MCP layer uses a different vocabulary
(`depends_on`, `explains`, `instance_of`, `learned_from`, `applies_to`, `implements`).
These are **not** in `VALID_RELATIONS` and will raise `ValueError` if passed to `add_edge`.
When bridging soul graph nodes into memory_graph, map or omit the edge type. A future
migration may unify the two vocabularies.
