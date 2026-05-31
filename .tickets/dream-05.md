---
id: dream-05
status: open
deps: ['dream-03', 'dream-04']
links: ['mem-08']
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: claude-dreaming-article
discovered_via: external_article_review
---
# Session-start priming + proactive recall of consolidated memory

**Article step 4 + §5.** "Claude pulls from the consolidated memory layer —
not raw logs — to shape behavior" and "surfaces relevant insights without being
asked." Wire dream artifacts into the hermes runtime's context assembly.

## Acceptance Criteria
- [ ] At session/turn start the gateway retrieves top-salience dream artifacts
      relevant to the user + task (medium-tier vector search, dream-03/04) and
      injects a compact "what I've learned" block into context — consolidated
      artifacts only, never raw session logs.
- [ ] Proactive surfacing: if a `failure_pattern` matches the current request,
      the agent is prompted to avoid it (the article's "without being asked").
- [ ] Bounded token budget + `MAC_DREAM_PRIMING` gate; retrieval increments the
      artifact `usage` counter feeding dream-04 salience.
- [ ] Test: a stored `decision_rule` for a task-type is retrieved and present in
      assembled context for a matching request.
