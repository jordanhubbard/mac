"""The observability -> action_events projection must store only a REFERENCE
to the source observability row, not a re-embedded copy of its `detail` blob.

Re-embedding `detail` made action_events a duplicate superset of
observability_events and was the write firehose that grew the hub mac.db to
16GB. These tests pin the deduplicated contract: the projected action event
keeps `observability_id` (so the detail is recoverable by join) but drops the
blob, and the source detail still lives in observability_events.
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
        level="info",
        detail={"secret_shaped": "x" * 512, "phase": "gate"},
    )

    action = _project(cp, obs)

    # The reference is present; the blob is not.
    assert action.attributes.get("observability_id") == obs.id
    assert "detail" not in action.attributes
    # Cheap scalar dimensions readers filter on are retained.
    assert action.attributes.get("layer") == "worker"
    assert action.attributes.get("kind") == "log"


def test_source_detail_remains_recoverable_via_observability_row() -> None:
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

    action = _project(cp, obs)
    ref = action.attributes["observability_id"]

    # Joining back on the reference recovers the full detail — nothing lost.
    source = [o for o in cp.list_observability(limit=50) if o.id == ref]
    assert len(source) == 1
    assert source[0].detail == {"clone_ms": 1200, "test_ms": 45000}


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
