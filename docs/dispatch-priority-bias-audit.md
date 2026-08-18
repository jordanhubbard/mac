# Dispatch priority bias ordering audit

> Historical audit, amended 2026-07-25. The original priority/FIFO analysis
> below remains useful, but dispatch now also applies a work-conserving class
> lane and bounded due-time aging. `urgent` and `recovery` outrank `normal`,
> while `background` yields; no worker is held idle when the higher lanes are
> empty. The candidate scan includes an explicit urgent/recovery window, and
> `task show` exposes the resolved class, age bonus, and due bonus.

Ground-truth finding for the parent repair
(`task_repair_6cd69ae5474dfb0f601ad5cd`). It traces how task-dispatch selection
orders by priority, whether a distinct "priority bias" concept exists, and where
the existing tests do and do not pin the behavior. No source ordering logic was
changed by this audit; the one code change is a gap-closing test (see
"Recommendation").

## Scope

Which open task an idle agent claims next, from the SQLite tasks table through
the Python dispatch ordering to the thin dispatch/api/worker surfaces:

- `src/mac/store.py` — `tasks.priority INTEGER NOT NULL DEFAULT 0` and the
  covering indexes `idx_tasks_state_priority ON tasks(state, priority DESC,
  created_at)` and `idx_tasks_review_queue ON tasks(priority DESC, created_at,
  id)`.
- `src/mac/services.py` — `_dispatch_candidate_tasks`, `_dispatch_ordered_tasks`,
  `_interleave_tasks_by_project`, `_rotate_by_page_prefix`,
  `_dispatch_task_sort_key`, `_dispatch_priority_age_bonus`, and the callers
  `dispatch_once`, `claim_next_for_agent`, `ready_tasks`, `list_tasks`.
- `src/mac/dispatch.py`, `src/mac/api.py`, `src/mac/worker.py` — priority
  passthrough only.

## (1) Exact ordering / tie-break rule and where it lives

The store defines the `priority` column and the `priority DESC, created_at`
indexes but issues **no** dispatch-time `ORDER BY priority` query itself. The
authoritative selection order is computed in `src/mac/services.py`:

- `_dispatch_candidate_tasks` reads a bounded candidate set as the union of
  windows over open, unowned, unleased tasks:
  `ORDER BY priority DESC, created_at, id LIMIT 500` (the hot high-priority
  window) unioned with `ORDER BY created_at, id LIMIT 500` (the oldest window,
  so an ancient low-priority task can still enter the scoring pass when the
  priority window is saturated), plus an explicit `urgent`/`recovery` metadata
  window. De-duplicated by id.
- `_dispatch_task_sort_key` is the real comparator. It returns the tuple
  `(-effective_priority, 0.0, created_at, id)` where
  `effective_priority = class_stride + priority + age_bonus + due_bonus`.
  Because the sort is ascending, negating the first field surfaces the higher
  priority first; `created_at` then `id` are ascending fallbacks (FIFO, then
  stable id). The second slot held a work-package critical-path order signal
  and is retained as a constant so the tuple arity is unchanged.
- `_dispatch_priority_age_bonus` adds `floor(age_seconds / step)` priority
  points, `step = MAC_DISPATCH_PRIORITY_AGING_SECONDS` (default 24h, floor 60s).
- `_dispatch_due_bonus` adds a bounded bonus after an optional `due_at` or
  `deadline_at` passes (default one point per four hours, capped at 24).
- `_dispatch_ordered_tasks` groups candidates by tenant, and
  `_interleave_tasks_by_project` round-robins each tenant's projects; within a
  project `_rotate_by_page_prefix` buckets by the leading
  `MAC_DISPATCH_PAGE_PREFIX_WIDTH` (default 2) characters of the page cursor/id
  and round-robins across buckets. Every group/bucket keeps its internal
  `_dispatch_task_sort_key` order, and single-bucket / single-project groups are
  no-ops, so the head still leads on `(priority, age)`.

Effective tie-break precedence: **dispatch class → effective priority
(priority + age bonus + due bonus) → created_at (oldest first) → id**, wrapped in
tenant/project/page-prefix fairness round-robins that only reorder *across*
groups, never within a same-key group.

## (2) Is "priority bias" a distinct concept, or IS priority ordering the bias?

Both, at different layers:

- Base ordering *is* the raw `priority DESC, created_at` rule (the index and the
  candidate query).
- A distinct, additive **bias** layer sits on top: the age-based bonus in
  `_dispatch_priority_age_bonus` (anti-starvation aging) plus the tenant/project
  interleave and page-prefix rotation fairness. These bias *which* eligible task
  wins beyond raw stored priority, so "priority bias" is not merely a synonym for
  the `ORDER BY`; it is the aging + fairness adjustment applied in Python.

## (3) Coverage gaps

Existing coverage (all currently passing) is substantial and lives mostly in
`tests/test_control_plane.py` and `tests/test_dispatch_advisor.py`:

- Priority order within a project, cross-project interleave fairness, and
  page-prefix rotation: `test_dispatch_preserves_priority_order_within_project`,
  `test_dispatch_ordered_tasks_*`, `test_rotate_by_page_prefix_*`,
  `test_ready_tasks_consume_page_prefix_rotation`.
- Sort-key precedence, priority>signal ties, aging bonus math, corrupt-timestamp
  tolerance, env overrides, and the candidate-window union:
  `test_dispatch_task_sort_key_*`, `test_dispatch_priority_age_bonus_*`,
  `test_dispatch_candidate_tasks_*`.
- Claim-path priority: `test_claim_next_prefers_high_priority_over_older_default_ready_task`,
  `test_dispatch_priority_aging_prevents_low_priority_starvation`.
- Work-package order-signal tie ordering:
  `test_current_compiled_critical_path_rank_orders_equal_priority_tasks`,
  `test_ordinary_task_order_falls_back_to_priority_aging_and_age`.

Gaps identified:

- **created_at FIFO tie-break was not pinned end-to-end.** The sort-key tests
  only asserted `created_at` is *present* at tuple index [2]; every end-to-end
  `_dispatch_ordered_tasks` ordering test used *distinct* priorities or an order
  signal, so nothing proved that two same-priority, no-order-signal peers
  actually dispatch oldest-first. This is the primary gap and is closed below.
- **Index vs query direction is intentional, not a bug.** `created_at` is stored
  as ISO-8601 UTC text; the aging bonus and candidate query both treat *older*
  (lexically smaller) as higher effective priority, so ascending `created_at`
  after `priority DESC` is the correct FIFO tie-break. `list_tasks` uses
  `priority DESC, created_at` (ascending) and matches the dispatch semantics;
  the keyword-search helper (`ORDER BY priority DESC, created_at DESC`) is a
  presentation-only listing, not a dispatch path, so its DESC secondary is not a
  dispatch mismatch. No coverage gap or defect here.
- Minor (not fixed here): no explicit assertion that
  `MAC_DISPATCH_TASK_WINDOW` is honored as the candidate cap, and no test that
  tenant-level interleave fairness composes with page-prefix rotation in the
  same pass. These are low-risk secondary paths.

## (4) Recommendation: add-tests

The ordering logic is correct and does not need a code fix; the meaningful
exposure is the untested created_at FIFO tie-break. Recommendation is
**add-tests**, scoped to a single regression that pins current behavior without
touching source ordering logic:

- `tests/test_control_plane.py::test_dispatch_ordered_tasks_breaks_priority_ties_by_created_at`
  — two equal-priority, no-order-signal tasks must dispatch oldest-first through
  `_dispatch_ordered_tasks`.

The remaining "minor" items are optional follow-ups and are not required to
close the audit.

## Consequential assumptions

- The keyword-search `created_at DESC` secondary sort is presentation-only and
  intentionally not a dispatch ordering; it was therefore excluded from the
  dispatch tie-break audit rather than flagged as a mismatch.
- "Priority bias" in the parent title maps to the additive aging bonus plus the
  tenant/project/page-prefix fairness rotation, not to a separately named
  configuration knob (none exists beyond the `MAC_DISPATCH_*` env overrides).
- The added regression pins *current* behavior (oldest-first FIFO); it is a
  characterization test, so it must be updated deliberately if the tie-break
  rule is ever intentionally changed.
