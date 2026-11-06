# memory_graph

SQLite-backed graph memory library for fleet agents. Stdlib only — no pip dependencies.

## Setup (per agent)

```python
import sys
sys.path.insert(0, '/path/to/mac/lib/memory_graph')
import memory_graph as mg
mg.init_db('/home/<user>/.mac/graphs/<name>/memory_graph.db')
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

## Relation types (VALID_RELATIONS)

Relations are typed — use the most specific one available; `related-to` is a last resort.

| Relation | Meaning |
|---|---|
| `related-to` | loose association (last resort — use something specific) |
| `cites` | this node references/quotes the target |
| `refines` | more precise version of a broader node |
| `corroborates` | independently confirms the target |
| `derived-from` | built on or computed from the target |
| `contradicts` | in known tension with the target |
| `depends_on` | requires the target to be true first |
| `explains` | provides the mechanism behind the target |
| `instance_of` | concrete example of an abstract principle |
| `learned_from` | attribute the source (person, event) |
| `applies_to` | scopes a rule to its domain |
| `implements` | this action enacts that principle |

## Per-agent setup

Each agent creates their own DB under `~/.mac/graphs/`:
```bash
mkdir -p ~/.mac/graphs/<name>
```

Then set MEMORY.md to:
```
Your memory lives in a graph. First tool call every session:
  import sys; sys.path.insert(0, '/path/to/lib/memory_graph')
  import memory_graph as mg; mg.init_db(os.path.expanduser('~/.mac/graphs/<name>/memory_graph.db'))
  for n in mg.top(8): print(f"[{n['kind']}] {n['summary']}")
Never write to this file again. The graph is the store.
```

## Gardener (daily maintenance)

```bash
python3 lib/memory_graph/gardener.py --db ~/.mac/graphs/<name>/memory_graph.db
```

Runs: decay, archive low-score, flag isolated nodes, detect near-dupes, surface promotion candidates, check skill-ref health.

Schedule with Hermes cron: `every day at 3am`.
