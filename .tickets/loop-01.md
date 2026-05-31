---
id: loop-01
status: open
deps: []
links: [mac-73cz, mem-08]
created: 2026-05-31T00:00:00Z
type: task
priority: 2
audit: autonomous-dev-loop-review
discovered_via: architecture_review
---
# Autonomous dev loop: architecture review + fail-closed hardening

End-to-end review of the autonomous dev loop and the fixes that make it stop
producing non-work that jams.

## Flow (as built)

1. **Claim** — `worker.py` polls `/agents/{id}/claim-next` every `poll_interval`
   (1s); gets a task + a 900s lease.
2. **Lease/heartbeat** — `_execute_with_lease_renewal` renews every
   `min(60, lease/2)`=60s. Hub lease TTL is also 900s (services.py:4498/6560) —
   **no TTL mismatch**; the earlier "~10s expiry" was the executor subprocess
   exiting fast on a provider 429, now bounded by [[mac-73cz]].
3. **Execute** — `mac-hermes-task-executor.py` (a heredoc in
   deploy-mac-fleet.sh) runs the vendored Hermes agent:
   `hermes_cli.main chat --query <prompt> --quiet --accept-hooks --yolo`. This
   IS agentic (max_turns=90, full toolset), so the agent can do multi-step dev
   work in a git worktree.
4. **Evidence** — for `publication_target=git://` tasks a deterministic git
   finalizer commits/pushes/tests and writes `mac-evidence.json` from REAL git
   state (honest). Otherwise the fallback writer ran.
5. **Verify → review → publish** — worker pre-check + server
   `_assess_default_review_evidence`/`validate_evidence_type` gate the evidence;
   a signed reviewer verdict then publishes (merges + `verify_source_ancestor`).

## Root causes fixed (this review)

- **The fallback writer fabricated verified completion.** It turned the agent's
  raw chat output (or its own "completed without textual output" placeholder)
  into a `status=complete` manifest with a synthetic passing
  `hermes_chat_query` check — so chatter ("hello hello hello") published as
  done. Fixed: the fallback now emits only an UNVERIFIED `operator_result`
  (never a fake repo_change/test) with no synthetic check.
- **`operator_result` accepted any non-empty text.** Added a substance gate
  (`_operator_result_is_substantive`) rejecting degenerate/placeholder text;
  genuine planning summaries (several distinct tokens) and structured
  findings/artifacts still pass.
- **Worker/server validator divergence.** The worker had its own
  `_worker_verification_contract_problems` whose operator_result branch lacked
  the gate, so chatter passed the local pre-check, was submitted, and crashed on
  the server's 400. Fixed by reusing the shared `_operator_result_is_substantive`
  so the worker fails the task cleanly (status=failed).
- Tests: `test_evidence_validators` (substance), `test_e2e_chatter_evidence_fails_closed`
  (full loop fails closed on chatter, passes on a substantive result),
  updated `test_deploy_agent_configs`.

## Remaining architectural debt (follow-ups)

- [ ] **Executor lives in a ~500-line bash heredoc** (deploy-mac-fleet.sh) —
      untestable, easy to drift. Extract to a real `src/mac/task_executor.py`
      module the deploy just invokes, with unit tests for the prompt builder,
      git finalizer, and fallback.
- [ ] **Full worker/server validator consolidation** — the worker still
      reimplements most of `evidence_validators` (`_worker_*`). Have the worker
      delegate to `validate_evidence_type` so the contract has ONE source of
      truth and cannot drift again.
- [x] **Worker loop resilience** — `run_forever` had no per-iteration guard, so
      a `run_once` re-raise (e.g. a server-only verification rejection past the
      local pre-check) crashed the whole worker and halted all autonomous work.
      Now `run_forever` catches it, records a `status=error` result, and keeps
      polling (run_once still best-effort-fails the task first). Test:
      `test_run_forever_survives_run_once_exception`. (A finer-grained
      clean-`failed`-on-4xx vs re-raise-on-5xx remains a possible refinement.)
