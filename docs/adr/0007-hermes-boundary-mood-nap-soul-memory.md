# ADR 0007 — Per-module ownership: mood_policy, nap_consolidator, soul_snapshot, memory_vetting

- Status: **Accepted**
- Date: 2026-06-19
- Decision owner: Jordan Hubbard
- Parent audit: task_836d585bf97347b6aec6710b51871941 (G5/7 — resolve the Hermes boundary inversion)
- Baseline commit: 3caea81

---

## Context

The `README.md` and `docs/hermes-boundary.md` state the canonical ownership split:

> **Hermes** owns personality, conversation, memory UX (SOUL.md, USER.md,
> MEMORY.md, skills, session memory, adaptive personality).
>
> **mac** owns operational truth (tasks, execution, evidence, routing,
> review, secrets, recovery).

Four modules in `src/mac/` sit athwart that boundary. They were ported or
grown inside `mac` but implement functions that belong, prima facie, to
Hermes's personality/memory/UX domain:

| Module | What it does |
|---|---|
| `mood_policy.py` | Renders emotional-tone prompt overlay text (warm/sad/enraged/…), ported verbatim from ACC InteractionPolicy |
| `nap_consolidator.py` | Authors structured "dream" memory artifacts (`record_type=dream:*`) and writes `nap_summary` records; operates during the agent's rest cycle |
| `soul_snapshot.py` | Pulls/pushes SOUL.md / USER.md / MEMORY.md across the fleet via SSH; calls `get_current_mood` and `list_personas` on the hub |
| `memory_vetting.py` + `memory_service.decay_memory` | Exports Qdrant vectors for operator vetting, deletes vetted points; implements TTL-based salience-weighted forgetting |

A G5/7 audit flagged the boundary as "inverted and leaky." This ADR
examines each module from first principles and decides: stay in mac,
move to Hermes, or formally reframe as mac's operational scope with the
contract made explicit.

### Why the boundary matters

When mac implements personality or memory UX logic, two failure modes
follow:

1. **Divergence**: Hermes carries its own version of the same logic for
   the same agent; the agent's effective behavior is determined by
   whichever copy ran last.
2. **Opaque coupling**: mac encodes assumptions about Hermes's internal
   file layout, mood model, and memory schema. Each upstream Hermes
   change risks silent misbehavior, exactly as the string-surgery shims
   documented in ADR 0001.

The goal is not to minimize mac's line count, but to eliminate hidden
behavioral contracts where each system assumes the other's internals
without an explicit interface.

---

## Decision

### Module 1: `mood_policy.py` — **Stay in mac; contract formally redefined**

**Finding:** `mood_policy.py` (69 lines) is a pure-function library that
converts a mood *token* (e.g. `"enraged"`) into a prompt-overlay *string*.
It carries no storage, no I/O, and no Hermes-internal dependencies. It
is used exclusively by `hermes_runtime.render_mood_section`, which is
already inside `src/mac/_hermes/` — the in-tree vendored Hermes snapshot
(ADR 0001). The mood *data* (the current mode + reason) lives in the mac
hub's `mood_overlays` table, set via `agent_state_service.set_mood`; mac
is the store. Rendering that stored token into prompt text is exactly the
kind of translation layer mac owns when it manages the vendored runtime
(ADR 0001).

**Redefined contract:** `mood_policy.py` is not Hermes personality logic
— it is *prompt-assembly infrastructure* for a mood state that mac stores
and serves. The public interface is `render_mood_overlay(mode, reason) ->
str`. Callers must treat the return value as opaque prompt text and must
not parse or branch on its content. The word-for-word mood descriptions
originated in ACC's InteractionPolicy and are preserved verbatim; any
revision is a product decision, not a mac-internal refactor.

**Non-action:** no code movement. The reframe is sufficient. Document the
contract in the module docstring (child task).

---

### Module 2: `nap_consolidator.py` — **Stay in mac; contract formally redefined**

**Finding:** `nap_consolidator.py` (612 lines) does two things:

1. Writes `nap_summary` memory records: flat, aggregate text of what the
   agent worked on since its last nap. This is *operational provenance* —
   a ledger of task activity grouped by task/project, written into the same
   `memory_records` table that holds deployment-learning records and other
   hub evidence.

2. Writes `dream:*` structured artifacts (decision_rule, failure_pattern,
   knowledge_snippet, tool_pattern, routing_signal). These are typed,
   evidence-backed, recall-indexed records derived from task execution
   history.

Both outputs are stored in mac's `memory_records` table, keyed to task IDs
and agent IDs, and are indexed into the mac-managed Qdrant medium tier.
The `_default_dreamer` is deliberately non-LLM (it is pattern-matching on
task metadata) and is pluggable — callers pass `dreamer_fn` for richer
processing.

What looks like "cognitive memory consolidation" (a Hermes concern) is
actually **task-execution summarization written to mac's operational
provenance store**. The nap window itself (`nap_runs` table) is mac
infrastructure. Dream artifacts use mac's `mac.dream.v1` schema, are
queried via mac's `/v1/memory/dreams/recall` endpoint, and feed back into
mac's dispatch decisions (e.g. `failure_pattern` records inform retry and
routing heuristics). Hermes's own memory consolidation (MEMORY.md rewrites,
session condensation) is a separate system.

**Redefined contract:** `nap_consolidator.py` operates on *operational
memory* — task provenance records indexed for fleet decision-making — not
on personal/conversational memory. Its output is the mac memory tier, not
Hermes's MEMORY.md. Dream artifacts are operational intelligence artifacts,
not personality or user memory. The pluggable `dreamer_fn` is the seam
where an LLM-backed enrichment can be substituted without tying core
consolidation logic to any Hermes internals.

**Boundary rule added:** `nap_consolidator` MUST NOT write to Hermes
`MEMORY.md` or any `HERMES_HOME` path. Its dream artifacts are readable
by Hermes via the hub `/v1/memory/dreams/recall` API (pull, not push).

---

### Module 3: `soul_snapshot.py` — **Partially stays in mac; hub-state capture requires formal interface contract**

**Finding:** `soul_snapshot.py` (380 lines) has two distinct sub-concerns
with opposite ownership profiles:

**Sub-concern A — fleet SSH snapshot (pull_snapshot, plan_and_push):**
Pull SOUL.md/USER.md/MEMORY.md from agents via SSH; diff and write them
back. This is an *operational runbook tool* — the same category as a
database backup script. It is operator-facing (`mac soul pull/push`), not
agent-facing. It does not interpret the soul files; it treats them as
opaque blobs. Ownership: **mac** (fleet operations tooling). No change
needed.

**Sub-concern B — hub-state capture (capture_hub_state):**
Calls `hub.list_personas()` and `hub.get_current_mood(agent_id)`. Persona
and mood data belong to Hermes's identity model; mac stores them as routing
metadata (the `personas` and `mood_overlays` tables) but their *semantic
authority* is Hermes. `capture_hub_state` is currently passed a duck-typed
`hub` parameter; in practice this is a `RemoteDispatch` surface that talks
to mac's own API. This is a best-effort snapshot — failures per agent are
silently swallowed — which is correct for operational tooling.

The risk is not ownership but interface stability: `capture_hub_state`
calls `hub.get_current_mood(agent_id)` and `hub.list_personas()`, which
are mac API methods. If those APIs change shape, the snapshot silently
produces stale or empty data. The fix is not to move the code to Hermes;
it is to assert the interface contract explicitly.

**Decision:**
- `pull_snapshot` / `plan_and_push` / `SSHTransport`: stay in mac as fleet
  operations tooling. No change.
- `capture_hub_state`: stays in mac but the `hub` parameter MUST satisfy a
  documented Protocol (add `SoulHubProtocol` with `list_personas()` and
  `get_current_mood(agent_id)` typed signatures). The function documents
  that it snapshots *mac-stored* persona and mood metadata, NOT the agent's
  live internal state — the distinction matters for operators reading the
  snapshot.

**Explicit non-goal:** `soul_snapshot.py` MUST NOT be extended to write
back to SOUL.md from mac-derived logic. The `plan_and_push` path is
operator-controlled (the operator edits local files and pushes). No
automated soul rewrite from mac event data.

---

### Module 4: `memory_vetting.py` + `memory_service.decay_memory` — **Stay in mac; these are operational infrastructure**

**Finding:**

**`memory_vetting.py` (112 lines):** Exports Qdrant vector points from
`mac_memory_medium` and `mac_memory_long` collections into a JSONL for
operator review, and deletes vetted point IDs. The collections it operates
on are mac-managed infrastructure (the Qdrant instance, collection names,
and embedding pipeline are all owned by mac's `VectorWriterService`). This
is a *data lifecycle management tool for mac's vector store*. The fact that
some points originated from agent personality summaries does not make the
pruning logic Hermes-owned; the vector store is mac's.

**`memory_service.decay_memory` (50 lines):** TTL-based pruning of
`memory_records` rows. It is salience-aware only in the heuristic sense —
it protects `PROTECTED_MEMORY_PREFIXES` (`deployment_learning`, `dream`,
`user`, `project`, etc.) and deletes everything else older than `ttl_days`.
This is lifecycle maintenance for mac's own `memory_records` table.
`PROTECTED_MEMORY_PREFIXES` is a mac-controlled allow-list; expanding it is
a mac operational decision, not a Hermes semantic decision.

What could belong to Hermes: deciding *which* records are semantically
important (salience scoring). What mac already owns: the table, the schema,
the TTL, the protected-prefix list, and the prune operation itself.

**Decision:** Both stay in mac. The existing design is correct. The only
clarification needed:

- `memory_vetting.py` is an operator tool for pruning mac's Qdrant store,
  not a Hermes memory lifecycle manager. Its `DEFAULT_COLLECTIONS` constant
  MUST track mac's canonical collection names; if collection names change,
  this must be updated in the same commit.
- `decay_memory` is mac-internal row pruning. The protected prefix list is
  intentionally conservative (false negatives preferred over false
  positives). Salience *scoring* (deciding which rows to protect based on
  semantic importance rather than record type prefix) is a future
  improvement that would live in mac.

---

## Summary Table

| Module | Decision | Rationale |
|---|---|---|
| `mood_policy.py` | **Stay in mac** — contract redefined as prompt-assembly infra | Pure function; operates on mac-stored mood state; consumed exclusively by mac's vendored Hermes runtime |
| `nap_consolidator.py` | **Stay in mac** — contract redefined as operational memory | Outputs go to mac's `memory_records` and Qdrant; dream artifacts feed mac dispatch; Hermes reads via API pull |
| `soul_snapshot.py` (SSH paths) | **Stay in mac** — fleet operations tooling | Operator-controlled, opaque blob transfer; no soul interpretation |
| `soul_snapshot.py` (capture_hub_state) | **Stay in mac** — add formal Protocol contract | Already uses mac's own API; needs typed interface to prevent silent drift |
| `memory_vetting.py` | **Stay in mac** — operational infrastructure | mac owns the Qdrant collections and prune authority |
| `memory_service.decay_memory` | **Stay in mac** — operational infrastructure | mac owns memory_records table; TTL + prefix list are operational, not semantic |

**No module moves to Hermes.** The initial audit finding of "inverted
boundary" was correct in diagnosis but overstated in prescription. The
modules are not Hermes personality logic accidentally placed in mac; they
are operational infrastructure whose subject matter *touches* personality
artifacts but whose locus of control is correctly mac-side. The inversion
is at the *contract* level: the modules lacked explicit interface
statements that distinguished "we store/render this data" from "we own the
semantics of this data."

---

## Consequences

**Positive:**
- Hermes retains full semantic authority over SOUL.md/USER.md/MEMORY.md
  content; mac's tooling treats those files as opaque blobs with no
  behavioral coupling to their content.
- Dream artifacts and nap summaries are queryable by Hermes via the hub
  API without Hermes importing mac internals; the pull-not-push discipline
  preserves the boundary.
- `mood_policy.py` is now formally a rendering library, not a personality
  engine; the personality engine is Hermes's SOUL.md / ACC-descended soul
  logic.
- Adding `SoulHubProtocol` closes the silent-drift risk in
  `capture_hub_state` without code movement.

**Negative / accepted cost:**
- These modules will need docstring and comment updates (child tasks
  G5/7-C and G5/7-D) to reflect the redefined contracts. They should not
  be refactored before those updates, as the refactor depends on the
  contracts being explicit.
- The `_default_dreamer` heuristic in `nap_consolidator.py` is not an LLM;
  in production a `dreamer_fn` should be injected. This is known technical
  debt, not a boundary violation.

**Explicitly rejected:**
- Moving any of the four modules to Hermes. The boundary clarification is
  at the contract level, not the file-location level. Cross-repo moves at
  this stage would create the very coupling they aim to avoid.
- Allowing `nap_consolidator` or `soul_snapshot` to write directly to
  Hermes's `HERMES_HOME` paths outside the operator-controlled SSH push
  flow.
- Expanding `mood_policy.py` with additional personality logic (new
  motivational tiers, soul overlays, etc.) — any such additions belong in
  Hermes's soul model and surface to mac only as a rendered string via the
  existing `render_mood_overlay` interface.

---

## Child tasks

This ADR explicitly does NOT implement code changes. Implementation is
tracked as:

- **G5/7-C**: Add `SoulHubProtocol` to `soul_snapshot.py`; update docstrings
  in all four modules to reflect the redefined contracts.
- **G5/7-D**: Add integration tests asserting the pull-not-push discipline
  (nap consolidator and dream artifacts do not write to HERMES_HOME paths).
