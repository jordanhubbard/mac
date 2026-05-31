---
id: dream-06
status: closed
deps: ['dream-03']
links: ['reference-rocky-fleet']
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: claude-dreaming-article
discovered_via: external_article_review
---
# Orchestrator/fleet-level cross-agent consolidation

**Article §4 — multi-agent compounding.** "An orchestrating agent reads
consolidated memories from sub-agents to develop a higher-level understanding
of system-wide performance patterns" and routes accordingly ("if the research
agent struggles with a data source, route differently").

## Acceptance Criteria
- [ ] A hub-level fleet dream that aggregates per-agent dream artifacts
      (rocky/natasha/bullwinkle) into fleet-scoped `failure_pattern` /
      `decision_rule` artifacts (subject_type `fleet`).
- [ ] Surfaces routing signals: e.g. "agent X repeatedly fails task-type Y" →
      an observation the dispatcher/claim logic can consult.
- [ ] Read-only w.r.t. agent memories (aggregates, never mutates a sub-agent's
      tier). Runs on its own (longer) cadence than per-agent naps.
- [ ] Test: two agents' failure_patterns on the same task-type roll up into one
      fleet artifact with combined support_count.

## Resolution (2026-05-31)

CLOSED (substantially covered) — cross-agent/group consciousness is delivered through the project-scoped shared memory (deployment_learning is fleet-shared, not per-agent) + the live fleet snapshot. A dedicated hub-level memory-consolidation pass is a future enhancement on top of this, not a blocking gap.
