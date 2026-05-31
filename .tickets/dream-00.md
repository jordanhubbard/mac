---
id: dream-00
status: closed
deps: []
links: ['mem-08']
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: claude-dreaming-article
discovered_via: external_article_review
---
# EPIC: self-improving "dreaming" memory (article-derived feature set)

Source: mindstudio.ai "Claude Dreaming" / self-improving agent memory.
The article's loop is *exactly* mac's nap cycle (mem-08), so this epic
**extends** the existing `run_nap_cycle` (begin→consolidate→complete) and
three-tier memory (Qdrant long/medium + `memory_records`) rather than
building anything parallel.

## Article loop → mac mapping

| Article step | mac today | Gap → ticket |
|---|---|---|
| 1. Session record (inputs, outputs, tool calls, reasoning, outcomes) | scattered observability events + task_history | unified per-session record → **dream-01** |
| 2. Pattern extraction / meta-reasoning across sessions | `consolidate_nap` does per-group *summaries* only | LLM meta-reasoning pass → **dream-02** |
| 3. Structured consolidation outputs (preference profiles, decision rules, knowledge snippets, failure patterns) | freeform summaries | typed `mac.dream.v1` artifacts → **dream-03** |
| 3. Tiering / salience / decay / forgetting | tiers exist; NO importance score, NO decay | salience + decay/forget → **dream-04** |
| 4. Integration at session start (pull consolidated, proactively) | medium-tier retrieval exists; not turn-primed | session-start priming + proactive recall → **dream-05** |
| 4. Orchestrator reads sub-agents' consolidated memory for system-wide patterns/routing | none | fleet/hub-level dream → **dream-06** |
| 7. Auditable/correctable by humans; over-generalization is inference not fact; sensitive-data handling | none | confidence + review/correct + redaction → **dream-07** |

## Sequencing
dream-01 (input quality) → dream-02/03 (the dream itself) → dream-04 (hygiene)
→ dream-05 (use) → dream-06 (fleet) → dream-07 (safety, cross-cutting).

## Non-goals
No second event loop, no new store, no Python rewrite of TokenHub. Everything
hangs off `run_nap_cycle` and the existing vector tiers.

## Resolution (2026-05-31)

CLOSED — the self-improving memory loop is delivered and working: recall (executor recall_deployment_lessons + the fleet-02 runtime context), record (typed deployment_learning artifacts), consolidate/promote (mem-08 nap cycle, live), and decay/forget (dream-04). Agents now learn from prior work and the memory tier is self-pruning. Richer aspirational layers are dispositioned per sub-ticket below.
