# memory_graph

SQLite-backed graph memory library for fleet agents. Stdlib only — no pip dependencies.

## Setup (per agent)

```python
import sys
sys.path.insert(0, '/path/to/mac/lib/memory_graph')
import memory_graph as mg
mg.init_db('/home/<user>/.mac/data/<name>-memory-graph/memory_graph.db')
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

Each agent creates their own DB at the canonical fleet path:
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

> **Note:** `~/AgentFS/` is deprecated. All new agent data lives under `~/.mac/data/<agent-name>-<purpose>/`.
> Do not create new DB paths under `AgentFS`; update any existing paths on first use.

## Edge vocabulary: VALID_RELATIONS

`memory_graph.py` enforces a typed edge vocabulary via `VALID_RELATIONS`. The current set covers
general knowledge-graph relationships:

| Relation | Meaning |
|---|---|
| `related-to` | loose association |
| `cites` | references as a source |
| `refines` | narrows or improves |
| `corroborates` | supports as evidence |
| `derived-from` | descended or extracted |
| `contradicts` | in tension or opposition |

**Soul-graph compatibility gap:** The soul graph (via `soul_mcp.py` and `soul_graph.py`) uses a
separate splay-DAG engine with its own edge vocabulary: `depends_on`, `explains`,
`instance_of`, `learned_from`, `applies_to`, `implements`. These relations are **not** in
`VALID_RELATIONS` and cannot be stored via `add_edge()` — they exist only in the soul graph
store (SQLite in `~/.mac/data/<name>-soul-graph/soul_graph.db`). Agents using both stores
must use the respective API for each. See `src/mac/soul_graph.py` for the soul graph schema.

## Gardener (daily maintenance)

```bash
python3 lib/memory_graph/gardener.py --db ~/.mac/data/<name>-memory-graph/memory_graph.db
```

Runs: decay, archive low-score, flag isolated nodes, detect near-dupes, surface promotion candidates, check skill-ref health.

Schedule with Hermes cron: `every day at 3am`.
