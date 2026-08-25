"""The exceptional observability -> action_events projection stores a reference.

Ordinary info logs and metrics stay only in observability_events. Mirroring
every telemetry row made action_events a duplicate indexed firehose. Warnings
and failures remain visible in the action feed, but carry only a reference to
their source detail.
"""

from __future__ import annotations

from mac.services import ControlPlane


def _project(cp: ControlPlane, obs):
    """Return the action_event projected from an observability event."""
    events = cp.list_action_events(action_type="observability", limit=50)
    match = [e for e in events if e.attributes.get("observability_id") == obs.id]
    assert len(match) == 1, "expected exactly one projected action_event"
    return match[0]


def test_projection_stores_reference_not_reembedded_detail() -> None:
    cp = ControlPlane.in_memory()
    obs = cp.record_observation(
        kind="log",
        name="worker.test",
        layer="worker",
        source="worker-1",
        level="warning",
        detail={"secret_shaped": "x" * 512, "phase": "gate"},
    )

    action = _project(cp, obs)

    # The reference is present; the blob is not.
    assert action.attributes.get("observability_id") == obs.id
    assert "detail" not in action.attributes
    # Cheap scalar dimensions readers filter on are retained.
    assert action.attributes.get("layer") == "worker"
    assert action.attributes.get("kind") == "log"


def test_metric_stays_only_in_observability_row() -> None:
    cp = ControlPlane.in_memory()
    obs = cp.record_observation(
        kind="metric",
        name="executor.duration",
        layer="executor",
        source="worker-2",
        value=46200.0,
        unit="ms",
        detail={"clone_ms": 1200, "test_ms": 45000},
    )

    actions = cp.list_action_events(action_type="observability", limit=50)
    assert not any(event.attributes.get("observability_id") == obs.id for event in actions)

    source = [o for o in cp.list_observability(limit=50) if o.id == obs.id]
    assert len(source) == 1
    assert source[0].detail == {"clone_ms": 1200, "test_ms": 45000}


def test_info_log_stays_only_in_observability_row() -> None:
    cp = ControlPlane.in_memory()
    obs = cp.record_observation(
        kind="log",
        name="worker.poll",
        layer="worker",
        source="worker-2",
        level="info",
    )

    actions = cp.list_action_events(action_type="observability", limit=50)
    assert not any(event.attributes.get("observability_id") == obs.id for event in actions)


def test_error_level_projects_failure_outcome_without_detail() -> None:
    cp = ControlPlane.in_memory()
    obs = cp.record_observation(
        kind="log",
        name="executor.crash",
        layer="executor",
        source="worker-3",
        level="error",
        detail={"traceback": "boom" * 100},
    )

    action = _project(cp, obs)
    assert action.outcome == "failure"
    assert "detail" not in action.attributes
