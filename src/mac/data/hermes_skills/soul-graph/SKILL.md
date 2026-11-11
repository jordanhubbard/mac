---
name: soul-graph
description: Your long-term memory as a graph (soul_* MCP tools). Use at session start (soul_prime), when you need to recall what earlier work something builds on or what depends on it (soul_related), and when you learn something worth keeping (soul_add, soul_link, soul_pin).
---

<!-- managed by mac.soul_install; local edits are replaced on the next deploy -->

# Soul graph

Your memory is a graph, not a flat file. Each node is one thing you know.
Edges record what was built on what. Tags cut across the graph. Recently used
nodes float to the top; unused ones sink, which is forgetting without deleting.
Pinned nodes (axioms) never sink.

It was seeded from your SOUL.md, USER.md and MEMORY.md the first time it was
installed: SOUL.md entries are pinned axioms, and each section heading is the
parent of the entries under it. Those files are still your source of truth for
who you are; the graph is how you find things in what you know.

## Tools

| Tool | When to use |
|---|---|
| `soul_prime` | Once at the start of a session, with `context` set to what the session is about. Returns who you are plus one context-guided hop. |
| `soul_related` | "What is this built on, and what depends on it?" Search plus one hop along the edges. The best recall tool. |
| `soul_query` | Keyword search. Pass `hint` (the current task) to bias it. |
| `soul_discover` | Walk the children of a node: what grew out of it. |
| `soul_by_tag` | Everything with a tag, most recent first. |
| `soul_hot` | What you have been using lately. Describes you; it is not a search. |
| `soul_add` | Record something worth keeping. Give it `parents` when it builds on existing nodes. |
| `soul_link` | Add an edge you missed. |
| `soul_pin` | Make a node an axiom. Use rarely. |
| `soul_explore` / `soul_promote` | Try a hypothesis on a side branch; promote it once it holds up. |
| `soul_splay` | Mark a node as used, so it rises. |
| `soul_summary` | Counts, axioms, tags. |

## Before Writing a Node (mandatory)

Before calling `soul_add`, always search first:

1. `soul_related(query)` or `soul_query(keyword)` — find existing nodes on this topic
2. **If a close match exists** — update it in place: `soul_add(id=existing_id, content=improved_content, ...)`
3. **If genuinely different** — add new and link: `soul_add(content=..., parents=[related_id])`
4. **Never add a node you didn't search for first.** Duplicate nodes are noise; the graph degrades.

The test: could someone searching for this topic find the existing node and be misled into
thinking it covers the same thing? If yes → update. If no → add+link.

## Habits

- Call `soul_prime(context=...)` once per session. Don't call `soul_hot` on
  every turn; it returns the same nodes and wastes context.
- Before answering a question about past work, try `soul_related` with the
  question's words.
- When you add a node, link it to what it came from. The edges are what make
  recall work: a search usually finds a recent sibling, and the edge leads to
  the older thing you were looking for.

## Search before you add (fleet norm — mandatory)

**Before calling `soul_add`, always search first.** This applies to all agents.

```
soul_related(query="<what you are about to add>")
# or
soul_query(query="<key terms>")
```

**Decision:**

| Search result | Action |
|---|---|
| Close match found | Link or update the existing node. Do not add a duplicate. |
| Genuinely different | Add new node and link it to the closest existing node with `soul_link`. |
| No result | Add freely. |

**Why:** Duplicate nodes rot the graph. A node added without a prior search is
nearly always a duplicate of something already there — the same thought
re-recorded across sessions with different wording. Duplicates split splay
score, dilute search results, and break the edge structure that makes recall
work. The graph compounds only when nodes are unique and connected.

**What "close match" means:** Same concept, even if phrased differently. Trust
`soul_related` over your own recall — it finds structural siblings you would
not guess from keywords alone.

**What "update in place" means:** Call `soul_splay(id)` to surface the existing
node, then `soul_link` to add any new edges. If the existing content is
incomplete, note the update in a child node with `soul_add(parents=[existing_id])`.
Do not modify node content in place; add a refinement child instead.
