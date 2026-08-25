"""Regression: ``mac task throughput`` stays bounded on a large live ledger.

The live v1.1.0 hub timed out servicing ``mac --json task throughput`` after
roughly thirty seconds while ``task stats`` and ``task ready`` returned
promptly.  The report explains up to fifty stranded ready-queue tasks, and each
explanation snapshotted every agent.  ``_v2_snapshot_agent`` fell back to the
single-agent :meth:`DispatchService._sync_barrier_state`, which scans the whole
non-terminal task set, so the report did ``O(stranded x agents x tasks)`` work.
On the ~7,000-task backlog that scan cost is exactly what blew past the CLI's
30s deadline.

The fix builds the project map and the bulk sync-barrier head map ONCE in
:meth:`ControlPlane.task_flow_report` and threads them through every
explanation, so the non-terminal scan runs a constant number of times
regardless of how many agents or stranded tasks the report covers.  These tests
count the scans and fail on the pre-fix per-agent-per-task fan-out.
"""

from __future__ import annotations

from datetime import timedelta

from mac.models import parse_time, utcnow
from mac.services import ControlPlane


def _fleet_with_stranded_ready_tasks(*, agent_count: int, task_count: int) -> ControlPlane:
    """Register ``agent_count`` idle agents and ``task_count`` OPEN tasks.

    Each freshly created OPEN task carries an open READY_QUEUE span.  Backdating
    those spans an hour makes every task read as stranded so the report explains
    it, which is where the pre-fix fan-out lived.
    """

    cp = ControlPlane.in_memory()
    machine = cp.register_machine("flow-host", resources={"cpu": 16, "memory_gb": 64})
    for index in range(agent_count):
        cp.register_agent(machine.id, "flow-worker-%d" % index, capabilities=[])
    for index in range(task_count):
        cp.create_task("stranded task %d" % index, project="flow")
    stale = (parse_time(utcnow()) - timedelta(hours=1)).isoformat(timespec="microseconds")
    with cp.store.transaction() as conn:
        conn.execute(
            "UPDATE task_flow_spans SET started_at = ? WHERE ended_at IS NULL",
            (stale,),
        )
    return cp


def _run_report_counting_scans(cp: ControlPlane) -> dict[str, int]:
    """Run the throughput report, counting the expensive non-terminal scans."""

    counts = {"non_terminal": 0, "single_agent_barrier": 0, "bulk_barrier": 0}
    original_non_terminal = cp._non_terminal_tasks
    original_single = cp.dispatch._sync_barrier_state
    original_bulk = cp.dispatch._sync_barrier_states

    def counted_non_terminal(*args, **kwargs):
        counts["non_terminal"] += 1
        return original_non_terminal(*args, **kwargs)

    def counted_single(*args, **kwargs):
        counts["single_agent_barrier"] += 1
        return original_single(*args, **kwargs)

    def counted_bulk(*args, **kwargs):
        counts["bulk_barrier"] += 1
        return original_bulk(*args, **kwargs)

    cp._non_terminal_tasks = counted_non_terminal  # type: ignore[method-assign]
    cp.dispatch._sync_barrier_state = counted_single  # type: ignore[method-assign]
    cp.dispatch._sync_barrier_states = counted_bulk  # type: ignore[method-assign]
    try:
        report = cp.task_flow_report(warning_seconds=1.0, critical_seconds=2.0)
    finally:
        cp._non_terminal_tasks = original_non_terminal  # type: ignore[method-assign]
        cp.dispatch._sync_barrier_state = original_single  # type: ignore[method-assign]
        cp.dispatch._sync_barrier_states = original_bulk  # type: ignore[method-assign]

    # Guard the fixture itself: the report must actually explain the stranded
    # ready-queue tasks, otherwise the scan counts below prove nothing.
    assert report["stranding"]["count"] > 0
    return counts


def test_throughput_report_scans_non_terminal_tasks_a_bounded_number_of_times():
    cp = _fleet_with_stranded_ready_tasks(agent_count=8, task_count=6)

    counts = _run_report_counting_scans(cp)

    # The bulk map is built once and reused; the single-agent scan -- the
    # ``O(agents)`` fallback that ran once per explained task pre-fix -- must
    # never run from the report path.
    assert counts["single_agent_barrier"] == 0
    assert counts["bulk_barrier"] == 1
    # One bulk scan for the sync-barrier map, at most one more inside
    # ``refresh_stale``'s scheduling snapshot -- and crucially not one per agent
    # per stranded task.
    assert counts["non_terminal"] <= 2


def test_throughput_report_scan_cost_is_independent_of_agent_count():
    small = _run_report_counting_scans(
        _fleet_with_stranded_ready_tasks(agent_count=2, task_count=6)
    )
    large = _run_report_counting_scans(
        _fleet_with_stranded_ready_tasks(agent_count=12, task_count=6)
    )

    # Pre-fix the non-terminal scan count grew with the agent count (agents x
    # stranded tasks). It must now be constant.
    assert small["single_agent_barrier"] == 0
    assert large["single_agent_barrier"] == 0
    assert large["non_terminal"] == small["non_terminal"]
