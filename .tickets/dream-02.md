---
id: dream-02
status: open
deps: ['dream-01']
links: ['dream-03']
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: claude-dreaming-article
discovered_via: external_article_review
---
# Meta-reasoning pass: extract recurring patterns in the nap

**Article step 2 — "meta-reasoning, thinking about its own thinking."**
Add an LLM pass inside `run_nap_cycle` (via the in-process gateway) that reads
the window's session records (dream-01) and emits *generalizable insights*:
request types seen often, approaches that succeeded vs. failed, edge cases
that caused confusion.

## Acceptance Criteria
- [ ] `consolidate_nap` gains a meta-reasoning step that calls the gateway
      (reuse the agent's resolved provider; cost-bounded, single call/nap) and
      returns typed candidates (feeds dream-03), not prose.
- [ ] Failure-safe: a gateway error during the pass is captured in
      `consolidation_error` and the nap still completes (mem-08 invariant —
      never strand the agent in DRAINING). Offline/no-provider → skip the pass,
      fall back to today's group summaries.
- [ ] The pass is gated by `MAC_DREAM_META_REASONING` (default off until proven)
      so it can be rolled out per-agent.
- [ ] Test: a window with 3 failed "push" sessions yields a candidate
      failure-pattern insight.
