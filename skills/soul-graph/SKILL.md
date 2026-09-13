---
name: soul-graph
description: Soul graph MCP tools — splay-DAG-tag memory retrieval. Use when querying, updating, or traversing agent soul/memory via MCP tools soul_query, soul_hot, soul_discover, soul_by_tag, soul_explore, soul_promote, soul_add, soul_link, soul_pin, soul_summary.
---

# Soul Graph

Agent memory as a living graph: splay ordering (recency floats nodes up),
DAG edges (honest causal lineage), tag cross-cuts, pinned axioms (never decay),
exploration branch (test hypotheses before committing to identity).

"The needful you did not know you were." — Reto Stamm, 2026-09-12

## MCP tools (all via `mac admin mcp serve` or `python -m mac.soul_mcp`)

| Tool | When to use |
|---|---|
| `soul_query` | Semantic search. Pass `hint=<current task>` for context-guided traversal |
| `soul_hot` | Top-N splay nodes — who this agent is right now |
| `soul_discover` | DAG walk from a seed — surfaces context you didn't ask for |
| `soul_by_tag` | Cross-cut on a tag (e.g. `axiom`, `insight`, `unresolved`) |
| `soul_splay` | Explicitly access a node, promoting it |
| `soul_pin` | Make a node an axiom — pinned nodes never decay |
| `soul_explore` | Add a hypothesis to the isolated sandbox branch |
| `soul_promote` | Graduate a hypothesis into the core graph |
| `soul_add` | Add a node directly with optional parents and tags |
| `soul_link` | Add a DAG edge between two existing nodes |
| `soul_summary` | Stats: node count, axioms, tags, exploration size |

## Context hint pattern

Always pass the current task as `hint` to `soul_query`. The graph biases
traversal toward the relevant neighbourhood before running the main query:

```
soul_query(query="trust", hint="debugging auth failure in fleet deploy")
```

## Axioms

Pin your core beliefs once. They stay at the root regardless of access
patterns. Everything else decays toward the leaves — forgetting is gravity,
not a purge.

## Exploration → promote lifecycle

1. `soul_explore(id, content)` — sandbox hypothesis, isolated
2. Use it. If it survives contact with reality:
3. `soul_promote(exp_id, parent_ids=[...])` — enters core graph with lineage

## Soul file

Default: `~/.hermes/soul.json`. Override with `--soul-file`.
Created empty on first use. JSON, committed to git — rollback is revert.

## Architecture note

`soul_mcp.py` is a thin MCP adapter over `soul_graph.SoulGraph` (PR #814).
The graph engine owns the data model; this file owns the tool surface.
Qdrant embeddings replace the keyword stub in `semantic_search` once
Rocky's follow-on PR lands. Until then, overlap scoring works for most cases.
