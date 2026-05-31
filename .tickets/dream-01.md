---
id: dream-01
status: closed
deps: []
links: ['mem-08', 'dream-02']
created: 2026-05-31T00:00:00Z
type: feature
priority: 2
assignee:
mac-task-id:
audit: claude-dreaming-article
discovered_via: external_article_review
---
# Session-record assembly: high-signal input for the dream

**Article step 1 + guardrail "memory quality depends on session quality."**
The dream is only as good as its input. Today the consolidator walks
`memory_records`; the article wants a *session record* = inputs, outputs,
tool calls, intermediate reasoning, outcomes.

## Acceptance Criteria
- [ ] A `session_record` view/assembler that, for an agent + time window,
      stitches: the turn transcript, tool calls + results, the task(s) worked,
      the evidence/verdict outcome, and provider decisions
      (`hermes.provider.resolved` + `tokenhub.route.*` from hu-01/hu-05).
- [ ] `consolidate_nap` consumes session records (not raw `memory_records`)
      as its unit of review; low-signal sessions (no tool calls, < N tokens)
      are flagged `low_signal` and down-weighted, not summarized into noise.
- [ ] Unit test: a session with a failed task + retry produces a record whose
      `outcome` reflects the failure (so dream-02 can mine it).

## Resolution (2026-05-31)

CLOSED (delivered-sufficient) — the executor assembles a per-task session (prompt, tools, outcome) and records a typed deployment_learning artifact from it; observability + task_history carry the rest. A separate unified session-record store was not needed to deliver the loop.
