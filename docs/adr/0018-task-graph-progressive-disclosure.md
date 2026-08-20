# ADR 0018 - The task view is a graph under progressive disclosure, not a board

- Status: **Proposed**
- Date: 2026-08-19
- Decision owner: MAC fleet owner
- Related: ADR 0013 (one authoritative hub allocator), ADR 0016 (agents decide
  what a task needs), `docs/task-dependency-semantics.md` (dependency failure
  semantics and required invariants)

## Context

The observability console (`observe/`) presents tasks as counts by state and as
per-state dwell. `StuckView` says it plainly in its own docstring: *"A state
whose p50 dwell is measured in days is not a queue, it is a graveyard."*

The console can say how many tasks are blocked. It cannot say **what blocks
them**, because the hub never sends the edges. `observe/src/lib/api.ts` has no
dependency field at all — the only `depth` it carries is merge-queue depth.

On 2026-08-19 the ledger held:

| State | Count |
| --- | --- |
| blocked | 355 |
| waiting | 19 |
| failed | 2,105 |
| cancelled | 3,531 |
| open | 84 |
| running | 2 |

`task_9f3b80b8` records that **165 of those blocked tasks wait on dependencies
that can never complete**. That is a third of the blocked population, dead, and
it took a hand-written query to find. A board cannot show it: on a board a
dead-blocked task and a task whose dependency lands in five minutes are the
same card in the same column. The distinguishing fact is not a property of the
task. It is a property of the *edge*.

### "The graph" is three different relations, and they must not be merged

This is the central scoping decision, and getting it wrong makes the view
unreadable:

1. **Containment** — parent to child, created by decomposition. `services.py`
   records a `dependency_graph` in `task.children_added` history when a parent
   decomposes. A **tree**: each child has one parent.
2. **Dependency** — task blocks task. A **DAG**, cross-cutting: a node may have
   many predecessors and many dependents, and edges routinely cross containment
   boundaries.
3. **Lineage** — retry and replacement. A task superseded by a retry is
   historically continuous with it but is a distinct row.

A view that draws all three as one relation produces a hairball. A view that
draws only the first is a prettier board.

### The join policy is part of the edge's meaning

`_dependency_join_policy_of()` (`task_ledger_audit.py:989`) resolves each task's
join to `all_success` (the default) or `all_settled` (cooperative integration
parents). The same edge from a **failed** dependency means different things:
under `all_success` the dependent is dead and will never run; under
`all_settled` it is released and will.

So "blocked by a failed task" is **not** sufficient to tell an operator whether
work is stuck. Any view that renders an edge without its join policy will
confidently mislabel dead work as pending, which is precisely the condition
that let 165 tasks accumulate unnoticed.

## Decision

Add a **graph view over a table spine**, in the console, read-only.

### 1. The table is the spine; the graph is disclosed progressively

The default view stays a table — sortable, scannable, keyboard-navigable, and
honest at 6,800 rows, which no node-and-edge canvas is. Each row carries a
disclosure triangle. Expanding a row reveals, indented beneath it:

- its **children** (containment), themselves expandable, and
- its **blockers** and **dependents** (dependency), rendered as distinct edge
  kinds with distinct affordances, never as more children.

Progressive disclosure is the right primitive because the operator's question
is almost always local: *why is this one not moving?* That question needs the
neighbourhood of one node, not a fleet-wide canvas.

### 2. Containment nests; dependency links

Disclosure triangles suit a tree and **do not** suit a DAG. A dependency edge
can point to a node that is already visible elsewhere, or upward, or into
another project; and dependency graphs can contain cycles, which is itself a
bug worth surfacing rather than a case to render.

Therefore: containment **nests** (indentation, recursive expansion), dependency
**links** (a row that names the other task and can focus it, re-rooting the
table on that node with a back path). No edge is drawn twice, and no attempt is
made to lay out a DAG as a tree.

A cycle is reported as a cycle, explicitly, not rendered.

### 3. Every edge carries its join policy and its liveness

An edge row states the blocker's state, the dependent's join policy, and the
resulting verdict — **satisfiable** or **dead**. "Blocked by `task_x` (failed;
join `all_success`) — this task can never run" is the sentence the console
cannot say today, and it is the whole point of the feature.

Dead-blocked subtrees are countable and filterable from the top of the view.

### 4. The existing honesty and read-only guarantees extend to edges

`observe/tests/readonly.test.ts` asserts the console issues only GET and HEAD,
routes every call through `src/lib/http.ts`, and contains no mutating verb
anywhere in the source tree. `observe/tests/honesty.test.tsx` asserts a missing
section says *unavailable* and never renders as `0`, and that "no transitions
happened" is distinguishable from "transitions unavailable".

Both extend unchanged. Specifically: a task whose edges could not be loaded
shows **edges unavailable**, never "no dependencies". Those are opposite facts,
and conflating them recreates the original bug in a new place.

### 5. The hub gains one endpoint, and it returns a bounded neighbourhood

The graph is served as the neighbourhood of a node to a requested radius, not
as a whole-ledger dump. 6,849 tasks in one project is already past what a
client should receive to answer "why is this blocked". The hub owns traversal,
cycle detection, and join-policy resolution, because it already owns them
(`task_ledger_audit._dependency_audit`) and a second implementation in
TypeScript would drift.

### 6. No graph-rendering dependency is added in the first increment

`observe/package.json` declares exactly two dependencies: `react` and
`react-dom`. The table-plus-disclosure design is deliberately chosen to need no
third. A visual canvas may later prove necessary for wide fan-out; if so it is
a separate ADR with its own justification, and it will be an additional view,
not a replacement for the table.

## Consequences

- The console answers "why is this task not moving" without a SQL prompt.
- Dead-blocked work becomes a number on a dashboard instead of a discovery.
  Expect that number to be **large** the first time it renders — 165 in one
  project — and expect it to read as a regression when it is a measurement.
- A bounded-radius endpoint means some questions ("show me every dead-blocked
  chain in the fleet") are aggregate queries, not traversals. That is a
  deliberate split, and the aggregate side is scoped as its own task.
- Deep chains still expand into long indented lists. Depth capping and "N more"
  affordances are required, and must state what was elided rather than
  silently truncating.

## Alternatives considered

**A node-and-edge canvas as the primary view.** Rejected for the first
increment. It is the obvious mental image of "task graph" and it fails at this
scale: 355 blocked tasks with cross-cutting edges is a hairball, it needs a
layout dependency, and it is hostile to keyboard navigation and to search — the
two things operators actually use. The table answers the common question
better.

**Extend the kanban with a "blocked by" column.** Rejected: a column shows one
hop. The 165 dead-blocked tasks were dead through *chains*, and one hop cannot
distinguish a blocker that is merely slow from one that is terminal.

**Ship the whole ledger to the client and traverse in TypeScript.** Rejected:
duplicates join-policy resolution that already exists in Python, and would
drift from it. `docs/task-dependency-semantics.md` defines required invariants;
one implementation should enforce them.

**Draw containment and dependency as one relation.** Rejected: they have
different shapes (tree vs DAG) and different meanings. Merging them makes
expansion ambiguous — the operator cannot tell whether an indented row is part
of this work or merely blocking it.
