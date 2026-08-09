"""A new sandbox image reaches each worker only after that worker drains.

The image goes out today through deploy-mac-fleet.sh, which pushes to nodes
from outside the control plane and has no idea what any worker is doing. A
worker mid-task gets its sandbox replaced underneath the task: the tools that
task resolved at start are gone, and it surfaces as the task misbehaving rather
than as a deployment that ran at the wrong moment.

A rollout is work that mutates the worker, so it is filed as a sync task --
which makes drain-then-update a property of the scheduler rather than of
whoever ran the deploy script.
"""

from __future__ import annotations

import json

import pytest

from mac.sandbox_rollout import (
    ROLLOUT_METADATA_KEY,
    RolloutError,
    plan_rollout,
    scheduled_rollouts,
    validate_image_ref,
)
from mac.services import ControlPlane

DIGEST = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64


@pytest.fixture()
def cp():
    plane = ControlPlane.in_memory()
    plane.create_project("mac", dispatch_paused=False)
    return plane


def _agents(count=2):
    return [{"id": "agent_%d" % index, "name": "worker%d" % index} for index in range(count)]


# --------------------------------------------------------------------------
# What may be rolled out
# --------------------------------------------------------------------------


def test_a_digest_is_accepted():
    assert validate_image_ref(DIGEST) == DIGEST


@pytest.mark.parametrize(
    "ref",
    [
        "ghcr.io/jordanhubbard/mac-openshell-runtime:latest",
        "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:short",
        "ghcr.io/someone-else/mac-openshell-runtime@sha256:" + "a" * 64,
        "",
        None,
    ],
)
def test_anything_but_the_reviewed_digest_is_refused(ref):
    """A tag can be repointed after review, so what ships and what was reviewed
    could differ with nothing recording it."""
    with pytest.raises(RolloutError):
        validate_image_ref(ref)


def test_the_refusal_explains_why_a_tag_is_not_enough():
    with pytest.raises(RolloutError) as excinfo:
        validate_image_ref("ghcr.io/jordanhubbard/mac-openshell-runtime:latest")

    assert "repointed" in str(excinfo.value)


# --------------------------------------------------------------------------
# Planning
# --------------------------------------------------------------------------


def test_every_agent_gets_its_own_barrier_task():
    plan = plan_rollout(_agents(3), DIGEST)

    assert len(plan) == 3
    assert {item["agent_id"] for item in plan} == {"agent_0", "agent_1", "agent_2"}


def test_each_rollout_is_a_sync_task_pinned_to_its_agent():
    """Both halves matter: sync without a target is refused at creation, and a
    target without sync would replace the sandbox under running work."""
    item = plan_rollout(_agents(1), DIGEST)[0]

    assert item["metadata"]["execution_mode"] == "sync"
    assert item["metadata"]["target_agent_id"] == "agent_0"


def test_a_busy_agent_is_still_rolled():
    """Skipping busy agents would roll the idle half of the fleet and leave the
    working half behind -- the workers doing the most work would be the ones
    running the oldest sandbox. Draining is the scheduler's job, not a filter
    applied here."""
    plan = plan_rollout([{"id": "agent_0", "name": "busy", "status": "busy"}], DIGEST)

    assert len(plan) == 1


def test_an_agent_already_scheduled_for_this_image_is_not_queued_twice():
    """A rollout re-filed each tick would queue a barrier per tick, and barriers
    quiesce their agent -- the worker would stop taking work permanently."""
    plan = plan_rollout(_agents(2), DIGEST, already_scheduled={("agent_0", DIGEST)})

    assert [item["agent_id"] for item in plan] == ["agent_1"]


def test_an_agent_scheduled_for_a_different_image_is_still_queued():
    older = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "b" * 64
    plan = plan_rollout(_agents(1), DIGEST, already_scheduled={("agent_0", older)})

    assert len(plan) == 1


def test_scheduled_rollouts_are_read_from_the_marker_not_the_title():
    tasks = [
        {"metadata": {ROLLOUT_METADATA_KEY: {"agent_id": "agent_0", "image": DIGEST}}},
        {"metadata": {"unrelated": True}},
        {"no_metadata": True},
    ]

    assert scheduled_rollouts(tasks) == {("agent_0", DIGEST)}


def test_the_task_records_the_bom_it_is_installing():
    """So a task that later fails on a missing tool can be checked against what
    the image it ran under was supposed to contain."""
    item = plan_rollout(_agents(1), DIGEST, bom={"packages": ["cmake"]})[0]

    assert item["metadata"][ROLLOUT_METADATA_KEY]["packages"] == ["cmake"]


def test_the_description_tells_the_worker_to_fail_rather_than_fall_back():
    """A half-rolled fleet that says nothing is worse than an unrolled one:
    nothing downstream can tell which sandbox a task actually ran in."""
    item = plan_rollout(_agents(1), DIGEST)[0]

    assert "FAIL the task" in item["description"]


# --------------------------------------------------------------------------
# Filing, through the real control plane
# --------------------------------------------------------------------------


def test_filing_creates_one_barrier_per_registered_agent(cp):
    machine = cp.register_machine("roll-host")
    cp.register_agent(machine.id, "worker1")

    report = cp.roll_out_sandbox_image(DIGEST, project="mac")

    assert len(report["filed"]) == 1
    filed = cp.get_task(report["filed"][0])
    assert filed.metadata["execution_mode"] == "sync"


def test_the_filed_task_quiesces_exactly_that_worker(cp):
    """The end of the chain: rollout -> sync task -> the agent stops taking
    async work until it has drained and updated."""
    machine = cp.register_machine("roll-host")
    agent = cp.register_agent(machine.id, "worker1")
    cp.roll_out_sandbox_image(DIGEST, project="mac")

    snapshot = cp.dispatch._v2_snapshot_agent(cp.get_agent(agent.id))

    assert snapshot.sync_barrier_pending


def test_rolling_twice_does_not_queue_a_second_barrier(cp):
    machine = cp.register_machine("roll-host")
    cp.register_agent(machine.id, "worker1")
    cp.roll_out_sandbox_image(DIGEST, project="mac")

    second = cp.roll_out_sandbox_image(DIGEST, project="mac")

    assert second["filed"] == []


def test_an_invalid_image_files_nothing(cp):
    machine = cp.register_machine("roll-host")
    cp.register_agent(machine.id, "worker1")

    with pytest.raises(RolloutError):
        cp.roll_out_sandbox_image("mac-openshell-runtime:latest", project="mac")

    assert not [task for task in cp.list_tasks() if "Sandbox rollout" in task.title]
