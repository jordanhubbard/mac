"""A generator that does not produce completed work stops filing.

Measured on the live ledger 2026-08-02 (7,781 tasks), by task origin:

    direct_task                 4,505 tasks   20.0%   (human, never gated)
    dream_low_confidence_repair 1,396 tasks    0.3%
    contract_prerequisite         599 tasks    7.7%
    environment_prerequisite      594 tasks    9.6%
    task_system_reset             137 tasks    2.9%
    self_heal                      54 tasks    0.0%
    crash_observer                 50 tasks    2.0%

The gate's job is to stop the bottom of that table without touching the top,
and to keep working for generators nobody has thought of yet.
"""
from __future__ import annotations

import pytest

from mac.generator_yield import (
    GeneratorSuppressed,
    GeneratorYieldGate,
    YieldPolicy,
    is_generator_origin,
)
from mac.models import TaskState
from mac.services import ControlPlane


def _file(cp, origin_type, *, count, completed, title="generated"):
    """File ``count`` tasks from ``origin_type``, completing ``completed``."""
    made = []
    for index in range(count):
        task = cp.create_task(
            "%s %d" % (title, index),
            project="mac",
            metadata={"origin": {"type": origin_type}},
        )
        made.append(task)
    for task in made[:completed]:
        cp.store.execute(
            "UPDATE tasks SET state = ? WHERE id = ?",
            (TaskState.COMPLETED.value, task.id),
        )
    return made


def _gate(cp, **kwargs):
    policy = YieldPolicy(
        enabled=True, min_sample=10, floor=0.05, cache_ttl_seconds=0, **kwargs
    )
    return GeneratorYieldGate(cp.store, policy)


def test_human_origins_are_never_gated():
    assert not is_generator_origin("direct_task")
    assert not is_generator_origin("operator_directive")
    assert not is_generator_origin("hermes_interaction")
    assert not is_generator_origin("")
    assert is_generator_origin("self_heal")
    assert is_generator_origin("crash_observer")
    # A generator nobody has written yet is gated by default. This is the
    # property that makes the rule survive the next generator.
    assert is_generator_origin("some_generator_invented_next_month")


def test_a_generator_below_the_floor_is_suppressed():
    cp = ControlPlane.in_memory()
    _file(cp, "self_heal", count=20, completed=0)
    gate = _gate(cp)

    verdict = gate.evaluate("self_heal")
    assert verdict["allowed"] is False
    assert verdict["reason"] == "below_yield_floor"
    assert verdict["filed"] == 20
    assert verdict["completed"] == 0

    with pytest.raises(GeneratorSuppressed) as excinfo:
        gate.enforce({"origin": {"type": "self_heal"}})
    assert "self_heal" in str(excinfo.value)


def test_a_generator_above_the_floor_keeps_filing():
    cp = ControlPlane.in_memory()
    # environment_prerequisite measured 9.6%, comfortably above a 5% floor.
    _file(cp, "environment_prerequisite", count=20, completed=2)
    gate = _gate(cp)

    verdict = gate.evaluate("environment_prerequisite")
    assert verdict["allowed"] is True
    assert verdict["reason"] == "above_yield_floor"
    assert gate.enforce({"origin": {"type": "environment_prerequisite"}}) is not None


def test_a_new_generator_may_earn_a_record_before_being_judged():
    cp = ControlPlane.in_memory()
    _file(cp, "brand_new_generator", count=3, completed=0)
    gate = _gate(cp)

    verdict = gate.evaluate("brand_new_generator")
    assert verdict["allowed"] is True
    assert verdict["reason"] == "insufficient_sample"


def test_human_filed_work_is_never_suppressed_however_bad_its_yield():
    cp = ControlPlane.in_memory()
    _file(cp, "direct_task", count=30, completed=0)
    gate = _gate(cp)

    verdict = gate.evaluate("direct_task")
    assert verdict["allowed"] is True
    assert verdict["reason"] == "not_a_generator"
    assert gate.enforce({"origin": {"type": "direct_task"}}) is None


def test_the_gate_blocks_creation_through_the_control_plane():
    """End to end: the choke point is create_task, not each generator."""
    cp = ControlPlane.in_memory()
    # File the record first under the default policy (min_sample 40), then
    # tighten the policy, so the fixture is not gated while building itself.
    _file(cp, "crash_observer", count=15, completed=0)
    cp.generator_yield_gate.policy = YieldPolicy(
        enabled=True, min_sample=10, floor=0.05, cache_ttl_seconds=0
    )

    with pytest.raises(GeneratorSuppressed):
        cp.create_task(
            "one more crash repair",
            project="mac",
            metadata={"origin": {"type": "crash_observer"}},
        )

    # The same ledger still accepts operator-filed work.
    assert cp.create_task("a human asked for this", project="mac") is not None


def test_the_gate_can_be_disabled():
    cp = ControlPlane.in_memory()
    _file(cp, "self_heal", count=20, completed=0)
    gate = GeneratorYieldGate(
        cp.store,
        YieldPolicy(enabled=False, min_sample=10, floor=0.05, cache_ttl_seconds=0),
    )

    verdict = gate.evaluate("self_heal")
    assert verdict["allowed"] is True
    assert verdict["reason"] == "gate_disabled"


def test_every_origin_can_show_its_own_yield():
    """'A generator that cannot be measured should not be allowed to file.'"""
    cp = ControlPlane.in_memory()
    _file(cp, "self_heal", count=20, completed=0)
    _file(cp, "environment_prerequisite", count=20, completed=2)
    _file(cp, "direct_task", count=10, completed=5)
    # Tighten only after the fixture exists, so filing it is not itself gated.
    cp.generator_yield_gate.policy = YieldPolicy(
        enabled=True, min_sample=10, floor=0.05, cache_ttl_seconds=0
    )

    report = cp.generator_yield_report()
    by_origin = {row["origin_type"]: row for row in report["origins"]}

    assert by_origin["self_heal"]["yield"] == 0.0
    assert by_origin["self_heal"]["allowed"] is False
    assert by_origin["self_heal"]["generator"] is True

    assert by_origin["environment_prerequisite"]["allowed"] is True
    assert by_origin["direct_task"]["generator"] is False

    # Worst-yielding generator first, so the thing to act on leads.
    assert report["origins"][0]["origin_type"] == "self_heal"


def test_suppression_is_recorded_rather_than_silent():
    cp = ControlPlane.in_memory()
    _file(cp, "self_heal", count=20, completed=0)
    cp.generator_yield_gate.policy = YieldPolicy(
        enabled=True, min_sample=10, floor=0.05, cache_ttl_seconds=0
    )

    with pytest.raises(GeneratorSuppressed):
        cp.create_task(
            "another self-heal finding",
            project="mac",
            metadata={"origin": {"type": "self_heal"}},
        )

    logged = [
        event
        for event in cp.list_observability(limit=200)
        if getattr(event, "name", "") == "task.generator_suppressed"
    ]
    assert logged, "a suppressed generator must leave a record"
    assert logged[0].detail["origin_type"] == "self_heal"
