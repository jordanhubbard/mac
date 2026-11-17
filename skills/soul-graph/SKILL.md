---
name: soul-graph
description: Soul graph protocol for all agents. Read before any soul_add call. Covers search-before-add, dedup, linking, and cross-agent visibility.
---

# Soul Graph Protocol

**Applies to: all fleet agents.**

The soul graph is a shared fleet knowledge store. Duplicate nodes fragment the
graph, dilute search relevance, and create cross-agent inconsistency. The
protocol below is mandatory — not advisory.

## Rule 1: Search before you add

**Before calling `soul_add`, always search first.**

```python
# Required sequence:
results = soul_related(query="<your concept>")        # semantic
# or:
results = soul_query(query="<keyword>")               # keyword
```

If a node with cosine similarity ≥ 0.7 (or clearly the same concept) is
returned: **do not add a new node**. See Rule 2.

If nothing close comes back: add the new node and link it. See Rule 3.

**Never skip the search step.** Not for speed, not for convenience.

## Rule 2: When a close match exists — update in place

A match means the concept is already in the graph. The right action depends on
what you have:

| Situation | Action |
|---|---|
| You have new evidence for the same concept | Extend or update the existing node's content via `soul_splay` to access it, then record the refinement in a child node linked with `relation='refines'` |
| You have a concrete instance of the concept | Add a narrow child node linked with `relation='instance_of'` |
| You reached the same conclusion via different reasoning | Add a child linked with `relation='converges_with'` |
| You have a contradiction | Add a node with `relation='contradicts'` — do NOT silently update |

```python
# Linking pattern:
soul_link(parent_id=existing_id, child_id=new_id, relation='refines')
```

Do NOT add a fresh root node with slightly different wording for the same
concept. That is graph rot.

## Rule 3: When genuinely different — add and link

If your search returned nothing close, your concept is novel:

1. `soul_add(content="...", tags=[...])` — mint the new node
2. If it relates to anything in the results (even at lower similarity): call
   `soul_link` with the most precise applicable relation
3. If it is a pure orphan with no connections: consider whether the tagging
   is too narrow — a well-tagged node will be found by future searches

Relations available: `contradicts`, `refines`, `depends_on`, `explains`,
`instance_of`, `learned_from`, `applies_to`, `implements`, `converges_with`.
Pick the most specific. `derived-from` (legacy) should not appear in new edges.

## Rule 4: Fleet-canonical nodes are auto-dedup targets

Nodes tagged `visibility:fleet-canonical` (all pinned nodes receive this tag
automatically) are compared at mint time. If your `soul_add` returns
`{deduped: true, existing_node: <id>}`, the sidecar found a canonical match —
call `soul_link` to register your edge and stop.

## Rule 5: Cross-agent visibility

The sidecar must run with `--pg-url` and `--cross-agent` for search to span
fleet nodes. If `soul_related` only returns your own agent's nodes, the sidecar
is misconfigured — check `ps aux | grep soul_mcp` for `--pg-url`. Do not add
"new" nodes that are actually peers' existing work.

## Diagnostic: verify your sidecar backend

```bash
ps aux | grep soul_mcp | grep -v grep
# Must see: --pg-url AND --cross-agent
# If missing either: search is local-only → dedup will miss peer nodes
```

## Quick reference

```
Before soul_add:
  soul_related(query) OR soul_query(query)
  ↓
  Match ≥ 0.7?  → soul_link (refines / instance_of / converges_with)
  No match?     → soul_add, then soul_link to best partial match (if any)
  Deduped?      → soul_link only
```
