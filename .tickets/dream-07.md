---
id: dream-07
status: open
deps: ['dream-03']
links: ['dream-05']
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: claude-dreaming-article
discovered_via: external_article_review
---
# Guardrails: confidence, human audit/correct, redaction

**Article §7.** Consolidations are *inferences, not facts*; they can
over-generalize from few examples; session logs hold sensitive data. Memory
must be auditable + correctable by humans.

## Acceptance Criteria
- [ ] Over-generalization guard: artifacts below a `support_count`/`confidence`
      floor are marked `provisional` and excluded from proactive surfacing
      (dream-05) until corroborated by more sessions.
- [ ] Human workflow: `mac dream list/show/correct/forget` to review artifacts,
      edit/annotate, or tombstone a wrong one (tombstones are respected by
      future naps so they don't resurrect).
- [ ] Redaction: a secret/PII scrub on session records before they enter the
      dream (reuse the evidence secret-scrub path); sensitive fields never land
      in a persisted artifact.
- [ ] Test: a provisional artifact is excluded from priming; a corrected
      artifact's edit survives the next nap; a tombstoned one is not recreated.
