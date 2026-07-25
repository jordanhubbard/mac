# Ground-Truth Audit: Dispatch Starvation-Boost / Priority-Aging Behavior

Read-only audit for the contract-repair of parent audit task
`task_00f23013f3c349ccac2e1e7474584c86` (title: "Audit dispatch starvation-boost /
priority-aging behavior and its contract-test coverage"). This document records ground
truth only; the audit changed **no** dispatch source or test logic. It confirms which
behaviors are correct and covered, and enumerates the coverage/correctness gaps the
remediation child should close, with exact `file:line` references.

## Summary Verdict

The dispatch starvation-boost / priority-aging behavior in `src/mac/services.py` is
**correct** and the primary paths are **well covered** by contract tests. However, the
coverage is **not complete**: several branches called out in the parent task are only
exercised indirectly or not at all, and one helper contains an effectively **dead
defensive branch** (`page_cursor`) that no live `Task` can reach. These are enumerated in
section 4 as actionable gaps.

## 1. Behaviors Confirmed Correct (src/mac/services.py)

Starvation protection is layered across three cooperating mechanisms on `ControlPlane`,
plus a bounded candidate window.

- Priority aging ("starvation boost"):
  - `_dispatch_priority_age_bonus` (src/mac/services.py:22834) converts age into whole
    aging steps via `max(0, int(age_seconds // step_seconds))` — monotonic non-decreasing
    in age, floored at 0. Correct.
  - `_dispatch_task_sort_key` (src/mac/services.py:22821) folds the bonus into
    `(-effective_priority, -order_signal, created_at, id)`. Correct precedence.
  - Step size `_DISPATCH_PRIORITY_AGING_SECONDS = 24h` (src/mac/services.py:22632),
    overridable via `MAC_DISPATCH_PRIORITY_AGING_SECONDS` through
    `_int_env(..., minimum=60)`. Correct.
  - Corrupt `created_at` is caught (bare `except`) and returns bonus `0` instead of
    raising (src/mac/services.py:22838-22842). Correct — dispatch is never aborted by bad
    data.

- Tenant -> project -> page-prefix round-robin interleave:
  - `_dispatch_ordered_tasks` (src/mac/services.py:22646) groups candidates by
    `_task_tenant_id` and round-robins tenants.
  - `_interleave_tasks_by_project` (src/mac/services.py:22678) round-robins projects within
    a tenant.
  - `_rotate_by_page_prefix` / `_page_prefix_key` (src/mac/services.py:22737 /
    src/mac/services.py:22726) round-robin page-prefix buckets within a project, preserve
    `(priority, age)` order inside a bucket, are a no-op for a single bucket, and never
    mutate the input list. Correct — no group can starve its siblings.

- Bounded working set:
  - `_dispatch_candidate_tasks` (src/mac/services.py:22790) unions a priority-ordered and
    an oldest-first window, each capped at `_DISPATCH_TASK_WINDOW = 500`, so the oldest
    tasks stay visible to the aging boost under backlog. Correct.

## 2. Important Semantic Clarification (correctness, not a bug)

The parent task phrases the env overrides as "minimum floors / minimum clamp". The actual
semantics of `_int_env` (src/mac/services.py:471) are **fall-back-to-default**, NOT
clamp-to-floor: an empty, unparseable, or below-`minimum` value returns the **default**,
not the floor. Verified empirically:

- `MAC_DISPATCH_PAGE_PREFIX_WIDTH=0` -> width `2` (default), not `1` (floor).
- `MAC_DISPATCH_PAGE_PREFIX_WIDTH=-5` -> width `2` (default).
- `MAC_DISPATCH_PRIORITY_AGING_SECONDS=0` -> `86400` (default), not `60` (floor).

This is safe (a misconfigured knob can never DISABLE the cap) and is the intended design,
but any test or doc describing it as a "clamp to the minimum" is inaccurate; it is a
"reject and use the safe default". The remediation child should keep this wording precise.

## 3. Behaviors Confirmed Covered by Contract Tests

`tests/test_control_plane.py`:
- Starvation boost end-to-end: `test_dispatch_priority_aging_prevents_low_priority_starvation`.
- Sort key: `test_dispatch_task_sort_key_orders_priority_then_signal_then_age`,
  `test_dispatch_task_sort_key_breaks_priority_ties_on_order_signal`,
  `test_dispatch_task_sort_key_age_bonus_lifts_effective_priority`.
- Age-bonus arithmetic + env override + corrupt timestamp:
  `test_dispatch_priority_age_bonus_counts_whole_aging_steps`,
  `test_dispatch_priority_age_bonus_env_override_shrinks_step` (covers below-floor `"1"` and
  unparseable `"not-an-int"` both falling back to the 24h default),
  `test_dispatch_priority_age_bonus_tolerates_corrupt_timestamp`.
- Candidate window: `test_dispatch_candidate_tasks_unions_priority_and_oldest_windows`.
- Tenant round-robin (via tick): `test_dispatch_tick_round_robins_between_tenants`.
- Project round-robin: `test_dispatch_round_robins_between_projects_within_tenant`.
- Page-prefix rotation: `test_rotate_by_page_prefix_round_robins_across_prefix_buckets`,
  `test_rotate_by_page_prefix_is_noop_for_single_bucket`,
  `test_rotate_by_page_prefix_preserves_priority_within_bucket`,
  `test_rotate_by_page_prefix_does_not_mutate_input`,
  `test_rotate_by_page_prefix_width_env_gated` (widths 1/2 + unparseable fallback),
  `test_dispatch_ordered_tasks_single_prefix_bucket_is_noop`,
  `test_dispatch_ordered_tasks_rotates_page_prefixes_within_project`,
  `test_ready_tasks_consume_page_prefix_rotation`.

`tests/test_work_package_assignment.py`:
- `test_ordinary_task_order_falls_back_to_priority_aging_and_age`.

Targeted run of these 42 tests: **42 passed** (exit 0).

## 4. Coverage / Correctness Gaps (for the remediation child)

1. Dead / untested `page_cursor` branch in `_page_prefix_key`
   (src/mac/services.py:22734). The line
   `source = getattr(task, "page_cursor", None) or task.id or ""` reads a `page_cursor`
   attribute, but `Task` (src/mac/models.py:930) has **no** `page_cursor` field
   (verified: `"page_cursor" in Task.__dataclass_fields__` is `False`). So for every real
   `Task` the `getattr` returns `None` and the branch falls through to `task.id`. The
   `page_cursor` path is therefore **unreachable dead code** and has **no test**. The
   docstrings/comments in `_rotate_by_page_prefix` and `_interleave_tasks_by_project`
   ("id/page-cursor prefix") advertise a behavior that cannot occur. Remediation options:
   (a) add `page_cursor` to the `Task` model and add a test that keys on it, or
   (b) simplify the helper to key on `task.id` only and drop the misleading page-cursor
   wording. Either way add an explicit test asserting the chosen behavior.

2. No **direct** unit test for `_page_prefix_key` (src/mac/services.py:22726). It is only
   exercised transitively through `_rotate_by_page_prefix`. A direct test (empty id,
   `width` larger than the key, `width=1`, and — if kept — the `page_cursor` branch) would
   pin the bucketing contract.

3. No **direct** test of `_interleave_tasks_by_project` or of tenant-level interleave
   ordering in `_dispatch_ordered_tasks` (src/mac/services.py:22646). Tenant round-robin is
   covered only indirectly via `test_dispatch_tick_round_robins_between_tenants` (which
   routes through `tick`/capability matching). A direct `_dispatch_ordered_tasks` test with
   multiple tenants (asserting cross-tenant round-robin ordering independent of the tick
   machinery) is missing.

4. No test of `_task_tenant_id`'s two branches (src/mac/services.py:26275): the
   `origin.tenant_id` branch vs. the top-level `tenant_id` branch vs. the `None` fallback.
   These drive tenant grouping in `_dispatch_ordered_tasks` and are only covered
   incidentally.

5. Env-override **below-floor** clamp for `MAC_DISPATCH_PAGE_PREFIX_WIDTH` is untested.
   `test_rotate_by_page_prefix_width_env_gated` covers `"1"`, `"2"`, and `"not-an-int"` but
   not a below-floor numeric value (`"0"` / `"-1"`). Given the fall-back-to-default
   semantics noted in section 2, a test asserting that `MAC_DISPATCH_PAGE_PREFIX_WIDTH=0`
   yields the default width (2), not the floor (1), would lock in the intended behavior and
   guard against a future accidental clamp-to-floor change.

## 5. Verification Performed

- Targeted contract slice (not the full gate, per the parent task): the 42 dispatch /
  aging / rotation tests listed in section 3 — **42 passed**, exit 0.
- Empirical confirmation of the section-2 fall-back semantics and the section-4 gap #1
  dead branch via a short `_int_env` / `Task.__dataclass_fields__` probe.

## 6. Conclusion

The starvation-boost / priority-aging behavior is correct and its main paths are covered.
The remediation child should (a) resolve the dead `page_cursor` branch (model it or remove
it) and align the docstrings, and (b) add direct tests for `_page_prefix_key`,
tenant-level `_dispatch_ordered_tasks`/`_interleave_tasks_by_project` round-robin,
`_task_tenant_id` branch selection, and the below-floor `MAC_DISPATCH_PAGE_PREFIX_WIDTH`
override. No source or test change was made by this audit.
