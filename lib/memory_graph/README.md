# memory_graph

SQLite-backed graph memory library for fleet agents. **Stdlib only** — no pip dependencies.

## Setup (per agent)

```python
import sys

sys.path.insert(0, "/path/to/mac/lib/memory_graph")
import memory_graph as mg

mg.init_db("/home/jkh/.mac/data/<name>-memory-graph/memory_graph.db")
```

> Note: use `~/.mac/data/<name>-memory-graph/` as the per-agent path.
> `~/AgentFS/` is deprecated — files there are invisible to other agents.

## API

### Core
- `init_db(db_path)` — create/upgrade schema (safe on existing DBs; additive only)
- `add_node(content, kind, tags, pinned, visibility, auto_link, title)` → node_id
- `add_edge(from_id, to_id, relation)` — typed relation
- `access(node_id)` — reinforces score (call when you use a node)
- `top(n, kind, exclude_tags, require_tags)` → highest-score nodes

### Search
- `search(query, limit)` → BM25 FTS5 results blended with splay score, falls back to LIKE
- `semantic_search(query_vector, top_n, model)` → cosine similarity over stored embeddings
- `browse(topic, top_n)` → `{topic, nodes, sub_topics}` — keyword-grouped navigation with co-occurrence clustering

### Enrichment
- `make_title(content)` → short title (first line / first sentence, 72 chars max)
- `extract_keywords(content, corpus_idf, top_n)` → TF-IDF keyword list
- `enrich_node(node_id, llm_fn)` — LLM enrichment hook; `llm_fn(content) → {title, keywords}`
- `embed_node(node_id, vector, model)` — store embedding blob (model-stamped cache)
- `backfill_titles_and_keywords(db_path)` — fill NULL title/keywords on existing nodes
- `rebuild_fts(db_path)` — rebuild FTS5 index from scratch

### Maintenance
- `fisheye(node_id, hops)` → focal + hop1 summaries + hop2 edge-type counts
- `decay_all(factor)` → exponential decay, pinned/axiom immune
- `node_count / pinned_count / axiom_count` — stats

### Feedback
- `gripe(text, source)` → one-call feedback node tagged `[feedback, open, source:<source>]`
  - CLI: `python3 memory_graph.py gripe "this is broken"`

## Node kinds
`memory`, `axiom` (always pinned), `skill-ref`, `task-ref`, `feedback`

## Edge relations
Classic: `related-to`, `cites`, `refines`, `corroborates`, `derived-from`, `contradicts`
Extended (soul-graph vocabulary): `depends-on`, `explains`, `instance-of`, `learned-from`,
`applies-to`, `implements`, `blocks`, `supersedes`

## Visibility tiers (via tags)
- `visibility:private` — agent-only (soul, sensitive data)
- `visibility:personal` — default
- `visibility:fleet` — shareable to other agents

## Search architecture

Three layers, in order of quality:

1. **FTS5 BM25** (primary) — full-text search over `title + keywords + content` with
   BM25 ranking blended with splay score (`0.6 × BM25 + 0.4 × splay`). Available via
   the `nodes_fts` virtual table, populated automatically via insert/update/delete triggers.

2. **Semantic search** (on-demand) — `semantic_search(query_vector)` performs cosine
   similarity over stored `embedding` blobs. Pure Python, no deps. Model-stamped cache:
   blobs from wrong model versions are skipped automatically.

3. **LIKE fallback** — used when FTS5 is unavailable (old SQLite build). Same AND-join
   behaviour as the original implementation.

## Keyword clustering / hierarchical navigation

```python
result = mg.browse("fleet", db_path=db)
# {
#   "topic": "fleet",
#   "nodes": [{"id", "title", "summary", "score", "keywords"}, ...],
#   "sub_topics": [{"keyword": "memory", "count": 3}, ...]
# }
```

`sub_topics` are keywords that co-occur with the topic in 2+ nodes — the natural
next level of a topic hierarchy, without any predefined taxonomy.

## Embedding hook

```python
# After computing an embedding externally:
mg.embed_node(node_id, vector_list, model="nvidia/nemotron-3.5-embed-8b", db_path=db)

# Semantic search:
query_vec = embed("fleet memory architecture")
results = mg.semantic_search(query_vec, top_n=5, model="nvidia/nemotron-3.5-embed-8b", db_path=db)
```

## LLM enrichment hook

```python
def my_llm(content):
    # call your LLM endpoint here
    return {"title": "...", "keywords": ["k1", "k2"]}


mg.enrich_node(node_id, my_llm, db_path=db)
```

## Migrating existing DBs

```python
mg.init_db(db_path)  # adds title/keywords/embedding columns + FTS5
mg.backfill_titles_and_keywords(db_path)  # fills NULL title/keywords for existing nodes
mg.rebuild_fts(db_path)  # syncs FTS5 index with current content
```

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
  for n in mg.top(8): print(f"[{n['kind']}] {n['title']}")
Never write to this file again. The graph is the store.
```

## Gardener (daily maintenance)

```bash
python3 lib/memory_graph/gardener.py --db ~/.mac/data/<name>-memory-graph/memory_graph.db
```

Runs: decay, archive low-score, flag isolated nodes, detect near-dupes,
surface promotion candidates, check skill-ref health.

Schedule with Hermes cron: `every day at 3am`.

## CLI

```bash
python3 memory_graph.py top [n]           # top-n by score
python3 memory_graph.py search <query>    # BM25 search
python3 memory_graph.py browse <topic>    # keyword navigation
python3 memory_graph.py gripe <text>      # file a complaint
python3 memory_graph.py backfill          # fill NULL titles/keywords + rebuild FTS
python3 memory_graph.py stats             # node/pinned/axiom counts
```
