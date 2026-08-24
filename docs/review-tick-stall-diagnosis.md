# Why the hub self-tick fails to drain the REVIEWING backlog

Diagnosis-only child of *"Unwedge review advancement without granting workers
admin scope"* (`task_1fbb72e19dc34956a06b4a309f418f0e`). This child makes **no
behavioral change** to the advancement logic; it establishes, with evidence,
*why* the wired hub path is not draining the 66 tasks frozen in `REVIEWING`
since `2026-07-29T22:46Z`, and hands each confirmed mechanism to a sibling fix
task. Characterization tests that pin each finding live in
`tests/test_review_tick_stall_diagnosis.py`.

The file:line anchors below are against the checkout this child was authored on.
The subsystem has been refactored since the parent task was filed (the review
sweep moved off `ControlPlane.tick()` onto a dedicated publication worker), so
the anchors in the task description no longer match; the mechanisms they
describe still exist and are re-anchored here.

## How advancement is wired now

- `ControlPlane.tick()` (`src/mac/services.py:17992`) no longer runs the review
  sweep inline. It short-circuits to `{"skipped": "runs_on_publication_worker"}`
  unless `MAC_TICK_RUNS_REVIEW_SWEEP=1` (`src/mac/services.py:18115`).
- The sweep runs on a dedicated daemon,
  `api._start_publication_worker` (`src/mac/api.py:4087`), which calls
  `_advance_default_review_sweep_page(..., allow_blocking_hub_verify=True)`
  (`src/mac/api.py:4140`). It is gated by
  `MAC_PUBLICATION_WORKER_INTERVAL_SECONDS`, whose default is `"30"` **only when
  `MAC_HUB_TICK_INTERVAL_SECONDS > 0`**, else `"0"` = off (`src/mac/api.py:4114`).
- Event-driven advancement (`enable_event_driven_review_advance`,
  `src/mac/services.py:2284`) is started from `_start_hub_tick_loop`
  (`src/mac/api.py:4064`) — i.e. it rides the **same** `MAC_HUB_TICK_INTERVAL_SECONDS`
  gate as the periodic tick.
- `_maybe_advance_reviews_on_heartbeat` (`src/mac/services.py:16563`) is now
  **default OFF** (`MAC_REVIEW_TICK_ON_HEARTBEAT`, default `"0"`,
  `src/mac/services.py:16573`).

## Confirmed root causes

### C1 — the non-blocking nudge is silently dropped unless the event consumer is running

When the sweep runs with `allow_blocking_hub_verify=False`, a task whose only
route forward is hub-side Option-C verification takes the else branch and calls
`self._nudge_review_workflow(task.id)` instead of verifying
(`src/mac/services.py:22034`).

`_nudge_review_workflow` (`src/mac/services.py:2372`) is a **no-op when
`self._advance_queue is None`**:

```
q = self._advance_queue
if q is None or not task_id:
    return
```

`_advance_queue` is initialised to `None` in the constructor
(`src/mac/services.py:2123`) and is only populated by
`enable_event_driven_review_advance` (`src/mac/services.py:2305`), which is
called **only** from `_start_hub_tick_loop` (`src/mac/api.py:4064`). Therefore,
in any process where `MAC_HUB_TICK_INTERVAL_SECONDS` is unset/`0` (CLI, test
suite, stateless replicas, or a hub whose tick loop is disabled), the nudge is
discarded and the task is neither verified nor re-queued — it can only be moved
by a *blocking* sweep. This is the "nudge silently dropped" mechanism.

Note the coupling that makes this brittle: the publication worker's own default
(`"30" if tick_interval > 0 else "0"`, `src/mac/api.py:4114`) and the event
consumer both hang off `MAC_HUB_TICK_INTERVAL_SECONDS`. A deployment that runs
the sweep worker with `allow_blocking_hub_verify=True` masks C1; but any code
path that reaches the non-blocking branch with the consumer down (the tick's own
`MAC_TICK_RUNS_REVIEW_SWEEP=1` escape hatch, or a heartbeat sweep) drops the
nudge on the floor.

*Pinned by:* `test_nudge_is_a_noop_without_the_event_consumer`,
`test_nonblocking_sweep_branch_only_nudges`,
`test_event_consumer_is_gated_by_the_tick_interval`.

### C2 — an uncapped `waiting_for_hub_verify` return traps a task whenever hub verify cannot produce a verdict

In `advance_default_review_workflow`, when Option-C hub verify is enabled and no
verdict evidence exists yet, the workflow returns
`status="waiting_for_hub_verify"` for any evidence that is `hub_verifiable`
(`src/mac/services.py:22089`). `hub_verifiable` is true whenever
`_hub_verify_repo_info(task, evidence)` returns non-None — i.e. the evidence is a
pushed repo change with a branch/head_sha (`src/mac/services.py:26594`).

There is **no attempt/iteration ceiling on this branch**: every sweep re-reads
the task, finds no verdict (because the verifier is broken, the reviewer key is
missing, the sandbox never converges, or the in-flight guard keeps firing), and
returns `waiting_for_hub_verify` again. The `_run_hub_review_verification`
in-flight guard (`src/mac/services.py:26886`, `review.id in inflight → return
None`) is per-process and unbounded; a verify that never completes keeps the id
parked and every subsequent tick short-circuits without producing a verdict. A
pushed code change therefore parks in `REVIEWING` indefinitely once the hub
verifier is unable to finish, which matches a large frozen cohort that shares a
single failing verify path. This is distinct from the "no evidence to verify"
case, which is deliberately *allowed* to fall through to the agent-nudge path
(`src/mac/services.py:22070` comment).

*Pinned by:* `test_waiting_for_hub_verify_has_no_iteration_ceiling`,
`test_hub_verifiable_evidence_holds_the_merge_gate`.

## Ruled-out hypotheses

### R1 — cursor starvation / durable-cursor parking (task hypothesis 3)

`advance_default_review_workflows` pages by `(priority DESC, created_at, id)` and
sets `next_cursor` **only when `has_more`** (`src/mac/services.py:21625`):

```
next_cursor = self._encode_review_sweep_cursor(tasks[-1]) if has_more and tasks else None
```

`_advance_default_review_sweep_page` then persists that cursor via
`reconciliation.complete(claim, cursor=result.get("next_cursor"))`
(`src/mac/services.py:21536`). When a page is the last page (`has_more` false),
`next_cursor` is `None`, so the durable cursor **resets to the start** on the
next claim — the sweep wraps rather than parking. With 66 non-advancing rows and
`MAC_REVIEW_TICK_LIMIT=25`, the cursor walks pages 1→2→3 and then wraps to the
head, so every row is revisited on a bounded cycle. The cursor is therefore a
progress mechanism, not a stall mechanism: it does not park a subset out of
reach. The freeze is per-task (C1/C2), not a cursor that skips rows. *Pinned by
`test_cursor_resets_to_head_when_no_more`,
`test_cursor_advances_monotonically_within_a_page_run`,
`test_last_page_persists_null_cursor`.*

### R2 — hub-agent name-vs-id mismatch / heartbeat gating (task hypothesis 4)

`resolve_hub_agent("MAC_REVIEW_TICK_HUB_AGENT")` (`src/mac/env_config.py:115`)
returns the configured value verbatim, and the heartbeat guard matches on
**both** name and id: `if agent.name != hub_agent and agent.id != hub_agent:
return` (`src/mac/services.py:16577`). With `MAC_REVIEW_TICK_HUB_AGENT=hub`,
an agent whose `name == "hub"` or whose `id == "hub"` passes. There is no
name/id mismatch defect here. The heartbeat path is, however, **default OFF**
(`MAC_REVIEW_TICK_ON_HEARTBEAT` default `"0"`, `src/mac/services.py:16573`), so
it is simply not a driver in the current deployment — advancement is expected to
come from the publication worker, not the heartbeat. This rules the heartbeat
matching out as the stall cause. *Pinned by
`test_hub_agent_matches_by_name_or_id`,
`test_heartbeat_review_tick_is_off_by_default`.*

### R3 — ordinary reconciliation lease expiry wedging the sweep (task hypothesis 2)

`ReconciliationCoordinator.claim` (`src/mac/reconciliation.py:47`) upserts the
`reconciliation_state` row and takes the lease only when
`lease_owner IS NULL OR lease_expires_at <= excluded.updated_at`. A crashed
holder that never calls `complete`/`abandon` leaves `lease_owner` set, so a
concurrent claim returns `None` and the sweep short-circuits with
`{"skipped": "lease_held"}` (`src/mac/services.py:21516`) — but **only until the
lease expires**. Expiry recovers it: the very next claim after
`lease_expires_at` passes the `WHERE` predicate and re-acquires. The lease is
bounded (`MAC_RECONCILER_LEASE_SECONDS`, clamped `1..3600`, default 60,
`src/mac/reconciliation.py:38`), so a stale lease can delay a page by at most one
lease interval, not permanently. `complete`/`abandon` release with the exact
`claim.owner_id` (which embeds a fresh per-call `new_id("claim")`), so a stale
owner cannot be "completed" by a different process — but expiry still frees it.
Ordinary lease expiry therefore does **not** wedge the sweep permanently, and is
ruled out. (A *permanent* wedge would require a live process re-claiming faster
than the backlog drains; that is C1/C2 keeping individual rows unadvanceable,
not the lease itself.) *Pinned by
`test_stale_lease_short_circuits_until_expiry`,
`test_expired_lease_is_reclaimable`,
`test_lease_release_requires_the_owning_claim_id`,
`test_lease_seconds_is_clamped`.*

## Recommended fix shape (for the sibling tasks)

1. **Decouple the nudge sink from the tick gate (C1).** Either start the
   event-driven consumer whenever the publication worker is enabled, or make the
   non-blocking sweep re-enqueue on the durable cursor rather than relying on an
   in-process queue that may be `None`. A dropped nudge must be observable — the
   no-op branch should record a diagnostic, not return silently
   (`src/mac/services.py:2378`).
2. **Bound the `waiting_for_hub_verify` loop (C2).** Add a per-review attempt
   ceiling / age cap so a verify that never converges retracts the review and
   re-selects a reviewer (mirroring the existing
   `reviewer_protocol_failure:hub_verdict_invalid` retraction at
   `src/mac/services.py:26884`) instead of returning `waiting_for_hub_verify`
   forever. Emit a warning once, not once per tick.

Both fixes are behavioral and belong to the parent's sibling fix tasks; this
child only characterizes and pins the current behavior.
