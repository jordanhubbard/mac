# Ground-Truth Audit: Dispatch Starvation-Boost / Priority-Aging Behavior

Read-only audit for the contract-repair of parent `task_00f23013f3c349ccac2e1e7474584c86`
(parent title: "Audit dispatch starvation-boost / priority-aging behavior and its
contract-test coverage"; contract `.mac/project.yaml`). This document records ground truth
only; the audit changed no dispatch source or test logic — the behavior and its coverage were
found already present, tracked, and passing at HEAD.

## Summary Verdict

The dispatch starvation-boost / priority-aging behavior is **already implemented, tracked, and
comprehensively covered in HEAD** (`264deff7945c3a4e80aae3976ae434cfa4681127`). Priority aging,
tenant/project round-robin interleaving, and page-prefix rotation each protect dispatch against
starvation, and every helper is exercised by dedicated contract tests. The canonical contract
gate (`scripts/run-contract-tests.sh`) passes cleanly (9055 passed / 4 skipped, exit 0) with the
statement floor (90.91% >= 90.00%) and branch floor (80.38% >= 80.00%) both satisfied.

## 1. Bootstrap

Command: `python3 scripts/bootstrap-project.py`

- Exit code: `0`
- Provisioned outputs present: `.venv/bin/python`, `.venv/bin/pytest`, `.venv/bin/coverage`.

## 2. Behavior Present in HEAD (`src/mac/services.py`)

Starvation protection is layered across three cooperating mechanisms, all in `ControlPlane`:

- Priority aging (the "starvation boost"):
  - `_dispatch_priority_age_bonus` (line ~22834) converts a task's age into whole aging steps,
    each worth one effective priority point, so an old low-priority task cannot starve behind a
    stream of fresh higher-priority work.
  - `_dispatch_task_sort_key` (line ~22821) folds that bonus into the sort key
    `(-effective_priority, -order_signal, created_at, id)`, i.e. priority (with age bonus), then
    work-package order signal, then FIFO age, then id.
  - Step size is `_DISPATCH_PRIORITY_AGING_SECONDS = 24h` (line ~22632), overridable via
    `MAC_DISPATCH_PRIORITY_AGING_SECONDS` through `_int_env(..., minimum=60)` so a misconfigured
    value can never DISABLE the cap — empty / unparseable / below-floor values fall back to the
    24h default (`_int_env`, line ~471).
  - Corrupt `created_at` is caught and yields a zero bonus rather than raising, so dispatch is
    never aborted by bad data.

- Tenant/project fairness interleaving:
  - `_dispatch_ordered_tasks` (line ~22646) groups candidates by tenant and round-robins tenants;
    `_interleave_tasks_by_project` (line ~22678) round-robins projects within a tenant so one
    project flooding high-priority tasks cannot starve its siblings for up to the aging period.

- Page-prefix rotation:
  - `_rotate_by_page_prefix` / `_page_prefix_key` (lines ~22737 / ~22726) bucket a project's
    candidates by the leading `_DISPATCH_PAGE_PREFIX_WIDTH = 2` characters of the page-cursor/id
    and round-robin across buckets so one id/page-cursor prefix cannot monopolize the scan window;
    it preserves `(priority, age)` order within a bucket, is a no-op for a single bucket, and does
    not mutate its input. Width is overridable via `MAC_DISPATCH_PAGE_PREFIX_WIDTH` with a floor
    of 1.

- Bounded working set:
  - `_dispatch_candidate_tasks` (line ~22790) unions a priority-ordered and an oldest-first window,
    each capped at `_DISPATCH_TASK_WINDOW = 500`, so the oldest tasks always remain visible to the
    aging boost even under a large backlog.

## 3. Contract-Test Coverage (`tests/`)

Priority-aging / starvation behavior is directly covered in `tests/test_control_plane.py`:

- `test_dispatch_priority_aging_prevents_low_priority_starvation` — end-to-end: an aged priority-0
  task is claimed ahead of a fresh priority-1 task.
- `test_dispatch_task_sort_key_orders_priority_then_signal_then_age`,
  `test_dispatch_task_sort_key_breaks_priority_ties_on_order_signal`,
  `test_dispatch_task_sort_key_age_bonus_lifts_effective_priority` — sort-key ordering incl. the
  age bonus raising effective priority.
- `test_dispatch_priority_age_bonus_counts_whole_aging_steps`,
  `test_dispatch_priority_age_bonus_env_override_shrinks_step` (incl. below-floor and unparseable
  overrides falling back to the safe default),
  `test_dispatch_priority_age_bonus_tolerates_corrupt_timestamp` — bonus arithmetic and safety.
- `test_dispatch_candidate_tasks_unions_priority_and_oldest_windows`,
  `test_dispatch_candidate_tasks_scopes_to_requested_project` — the bounded candidate windows.
- Interleave / rotation: `test_dispatch_tick_round_robins_between_tenants`,
  `test_dispatch_round_robins_between_projects_within_tenant`,
  `test_dispatch_preserves_priority_order_within_project`,
  `test_rotate_by_page_prefix_round_robins_across_prefix_buckets`,
  `test_rotate_by_page_prefix_is_noop_for_single_bucket`,
  `test_rotate_by_page_prefix_preserves_priority_within_bucket`,
  `test_rotate_by_page_prefix_does_not_mutate_input`,
  `test_rotate_by_page_prefix_width_env_gated`,
  `test_dispatch_ordered_tasks_single_prefix_bucket_is_noop`,
  `test_dispatch_ordered_tasks_rotates_page_prefixes_within_project`.

`tests/test_work_package_assignment.py` corroborates the fallback ordering:
- `test_ordinary_task_order_falls_back_to_priority_aging_and_age`.

Targeted run of the 21 aging/starvation/rotation tests: `21 passed`, exit 0.

## 4. Canonical Verification Gate

Command: `scripts/run-contract-tests.sh`

- Exit code: `0`
- Bulk slice: `9055 passed, 4 skipped, 25 warnings`.
- Focused re-run slice: `4 passed, 39 skipped, 9059 deselected`.
- Coverage floors satisfied: statements `90.91% (67758/74530) >= 90.00%`; branches
  `80.38% (19616/24404) >= 80.00%`.

## 5. Conclusion

No source or test change was required for the parent deliverable: the starvation-boost /
priority-aging behavior and its contract-test coverage are already present, tracked, and passing
at HEAD. The only remaining work for the parent contract prerequisite was to run the canonical
gate to green and record this ground-truth audit, which this document does.
