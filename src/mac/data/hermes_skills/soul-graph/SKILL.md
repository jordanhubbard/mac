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

## Habits

- Call `soul_prime(context=...)` once per session. Don't call `soul_hot` on
  every turn; it returns the same nodes and wastes context.
- Before answering a question about past work, try `soul_related` with the
  question's words.
- When you add a node, link it to what it came from. The edges are what make
  recall work: a search usually finds a recent sibling, and the edge leads to
  the older thing you were looking for.
