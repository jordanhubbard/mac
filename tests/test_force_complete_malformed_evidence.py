"""Malformed evidence must not make an endpoint unusable.

task_6c29f908 was failed for missing publication proof, then given valid pushed
repo evidence, RTX test evidence and deployment evidence. `mac task reopen`
succeeded; every subsequent `mac task force-complete` returned HTTP 500, for
both `codex` and `agent_rocky`. The operator filed task_4bfeab06.

The cause was one line. ``_require_canonical_integration_proof`` walks the
task's evidence looking for a durable canonical push, and reads each record
with ``ensure_json_object``, which did ``dict(value)`` for anything non-None.
One record carried a ``repo`` field that was not an object, so:

    ValueError: dictionary update sequence element #0 has length 1; 2 is required

ValueError is not a domain error, so it surfaced as a bare 500 and the task
could never be completed by any actor.

Evidence manifests are agent-supplied JSON. A field documented as an object can
arrive as a string, a list or a number, and the right response is to treat that
record as unconvincing -- not to abort the scan. A malformed record is exactly
the kind of thing the proof requirement exists to reject.
"""

from __future__ import annotations

import pytest

from mac.models import ValidationError, ensure_json_object
from mac.services import ControlPlane


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        ["repository_url"],          # the shape that caused the outage
        "postgresql://host/db",
        7,
        [["a", 1], ["b"]],
        ("a", "b"),
        set(),
    ],
)
def test_a_non_object_becomes_an_empty_object(value):
    """"Not an object" and "no object" are the same answer to every caller."""
    assert ensure_json_object(value) == {}


def test_a_mapping_is_still_copied():
    source = {"head_sha": "abc", "branch": "main"}
    result = ensure_json_object(source)

    assert result == source
    result["head_sha"] = "mutated"
    assert source["head_sha"] == "abc", "callers must not alias the original"


def test_none_is_still_an_empty_object():
    assert ensure_json_object(None) == {}


# --------------------------------------------------------------------------
# The endpoint behaviour that was broken
# --------------------------------------------------------------------------


def _claimed_repo_task(cp):
    """A repo-contract task with an active lease, so evidence can be authored.

    add_evidence requires the author to hold the lease; that is the real path
    an executing agent takes, so the tests take it too.
    """
    machine = cp.register_machine("evidence-host")
    agent = cp.register_agent(machine.id, "evidence-worker", capabilities=["python"])
    task = _repo_task(cp)
    cp.claim_task_v2(task.id, agent.id, lease_seconds=600)
    return task, agent


def _repo_task(cp):
    """A task carrying a repository execution contract, as ova's tasks do."""
    return cp.create_task(
        "repo work",
        project="mac",
        metadata={
            "origin": {"repository_url": "https://github.com/o/r"},
            # type=repository plus a repository_contract is what makes
            # _repository_contract_for_task engage the canonical-proof gate.
            # Without both, completion is unconditional and the gate this
            # regression lives in never runs.
            "execution_contract": {
                "type": "repository",
                "evidence_type": "repo_change",
                "quality": "strong",
                "repository_required": True,
                "repository_contract": {
                    "project": "mac",
                    "canonical_branch": "main",
                    "canonical_remote_url": "https://github.com/o/r",
                },
            },
        },
    )


def test_malformed_evidence_does_not_crash_the_completion_gate():
    """The regression: one bad record used to abort the whole scan."""
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    task, agent = _claimed_repo_task(cp)
    cp.add_evidence(
        task.id,
        kind="repo_change",
        uri="https://github.com/o/r/pull/1",
        summary="pushed",
        created_by=agent.id,
        metadata={"verification": {"repo": ["repository_url"]}},
    )

    # A domain error is correct here -- there genuinely is no valid proof.
    # ValueError/500 is not.
    with pytest.raises(ValidationError):
        cp.force_complete_task(task.id, "operator", "reconcile")


def test_a_valid_proof_after_a_malformed_record_still_completes():
    """A bad record must not hide a good one later in the list.

    This is the operator's actual situation: valid pushed repo evidence was
    added, and the endpoint still refused.
    """
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    task, agent = _claimed_repo_task(cp)
    sha = "a" * 40

    cp.add_evidence(
        task.id,
        kind="log",
        uri="mac://log/1",
        summary="noise with a non-object repo field",
        created_by=agent.id,
        metadata={"verification": {"repo": ["repository_url"]}},
    )
    cp.add_evidence(
        task.id,
        kind="repo_change",
        uri="https://github.com/o/r/pull/1",
        summary="the real proof",
        created_by=agent.id,
        metadata={
            "verification": {
                "repo": {"head_sha": sha, "branch": "main"},
                "canonical_integration": {
                    "status": "pass",
                    "remote_verified": True,
                    "canonical_ref": "refs/heads/main",
                    "canonical_tip_sha": sha,
                },
            }
        },
    )

    completed = cp.force_complete_task(task.id, "operator", "reconcile")

    assert completed.state == "completed"


def test_reopen_then_force_complete_is_possible():
    """The operator sequence: a stuck task reopened, then reconciled.

    reopen is valid from blocked/failed and deliberately refuses a COMPLETED
    task, so this drives it through blocked -- the state the real task
    (task_6c29f908) passed through on its way to failed.
    """
    cp = ControlPlane.in_memory()
    cp.create_project("mac", dispatch_paused=False)
    task = cp.create_task("plain work", project="mac")
    machine = cp.register_machine("reopen-host")
    agent = cp.register_agent(machine.id, "reopen-worker", capabilities=["python"])
    assignment = cp.claim_task_v2(task.id, agent.id, lease_seconds=600)
    lease_id = getattr(assignment, "lease_id", None) or cp.get_task(task.id).lease_id
    cp.transition_task(
        task.id, "blocked", agent.id, {"reason": "stuck"}, lease_id=lease_id
    )

    reopened = cp.reopen_task(task.id, "operator", "needs rework")
    assert reopened.state == "open"

    completed = cp.force_complete_task(task.id, "operator", "reconciled out of band")
    assert completed.state == "completed"
