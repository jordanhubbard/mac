---
id: dream-03
status: open
deps: ['dream-02']
links: ['dream-04', 'dream-05']
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: claude-dreaming-article
discovered_via: external_article_review
---
# Typed consolidation artifacts (mac.dream.v1) + medium-tier embedding

**Article step 3 — structured outputs, not raw logs.** Define a schema and
persist four artifact kinds the article names:
1. `preference_profile` — per user / task-type preferences.
2. `decision_rule` — refined "when X, do Y" rules.
3. `knowledge_snippet` — curated reusable facts.
4. `failure_pattern` — flagged things to avoid.

## Acceptance Criteria
- [ ] `mac.dream.v1` schema (validated like the evidence manifests) with the
      four kinds, each carrying `confidence`, `support_count` (how many
      sessions), `source_session_ids`, `created_by`, `nap_run_id`.
- [ ] dream-02 candidates are written as typed `memory_records`
      (record_type `dream:<kind>`) and embedded into the **medium** tier so
      retrieval (dream-05) can pull them.
- [ ] Idempotent: re-running a nap over the same window updates/merges
      artifacts by stable key, doesn't duplicate (cf. mem-05 idempotency).
- [ ] Test: round-trips all four kinds; invalid kind rejected by the validator.
