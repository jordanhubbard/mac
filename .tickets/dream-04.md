---
id: dream-04
status: closed
deps: ['dream-03']
links: ['project-mac-observability-bloat']
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: claude-dreaming-article
discovered_via: external_article_review
---
# Salience scoring + decay/forgetting across the memory tiers

**Article §3 gap.** mac has long/medium tiers but no importance score, no
decay, no forgetting — so the tier only grows (cf. observability bloat). Add
the hygiene the article implies ("distill noise into high-signal knowledge").

## Acceptance Criteria
- [ ] Each dream artifact (dream-03) gets a `salience` score from
      support_count × recency × confidence × usage (incremented when dream-05
      actually retrieves it).
- [ ] A decay job (runs inside the nap) demotes stale low-salience artifacts
      medium→archive and prunes archive past a TTL; high-salience artifacts are
      promoted/kept in long tier.
- [ ] Bounded + reversible: a `MAC_DREAM_FORGET_DRY_RUN` mode reports what would
      be forgotten without deleting; counts are emitted as observations.
- [ ] Test: a never-retrieved low-confidence artifact decays below threshold and
      is archived after the simulated TTL; a frequently-retrieved one survives.

## Resolution (2026-05-31)

CLOSED — delivered. Salience-aware decay/forgetting implemented: ControlPlane.decay_memory / `mac memory decay` (dry-run by default, bounded, --apply to prune) forgets stale low-salience records while protecting curated knowledge (user/project/feedback/deployment_learning/beads_memory). Also addresses the memory-tier bloat. Tests: test_memory_decay.py.
