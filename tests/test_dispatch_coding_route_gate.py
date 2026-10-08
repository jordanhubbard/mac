"""Every assignment path asks the same coding-route gate.

With ``MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT`` on, repository work goes
only to a worker with a fresh proof for a CLI on the hub's list. The gate used
to run on the legacy claim path only, so ``dispatch_once`` (the allocator)
handed a REGISTERED project's work to a worker with no usable proof. These
tests register the project on purpose: an unregistered one is refused for an
unrelated reason, which is how the old gate tests passed without the gate.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from mac.coding_route_gate import (
    MODEL_UNVERIFIED,
    ROUTE_UNREPORTED,
    ROUTE_UNVERIFIED,
    CodingRouteProof,
    proof_from_resources,
    refusal,
)
from mac.models import ValidationError, parse_time, utcnow
from mac.services import ControlPlane

PROJECT = "repo-beads-mac"


def _repo_metadata(**extra):
    return {
        "origin": {
            "type": "direct_task",
            "repository_contract": {
                "schema": "mac.repository_contract.v1",
                "project": PROJECT,
                "platforms": ["darwin", "linux", "wsl2"],
                "toolchain": {"required_commands": ["git"]},
                "test": {"command": "scripts/run-contract-tests.sh"},
            },
        },
        **extra,
    }


def _route(cli="opencode", *, verified=True, model="", checked_at=None):
    fingerprint = "sha256:route-proof"
    return {
        cli: {
            "configured": True,
            "verified": verified,
            "model": model,
            "route_fingerprint": fingerprint,
            "verification": {
                "verified": verified,
                "checked_at": checked_at or utcnow(),
                "model": model,
                "route_fingerprint": fingerprint,
            },
        }
    }


def _resources(clis=None, *, schema="mac.coding_clis.v2"):
    resources = {
        "openshell_required": True,
        "commands": {"schema": "mac.command_inventory.v1", "available": ["git"]},
    }
    if clis is not None:
        resources["coding_clis"] = {"schema": schema, "clis": clis}
    return resources


def _stale():
    return (parse_time(utcnow()) - timedelta(hours=2)).isoformat()


#: (case, resources, task metadata extra, expected refusal)
REFUSED = [
    ("unverified", _resources(_route(verified=False)), {}, ROUTE_UNVERIFIED),
    ("unlisted_cli_only", _resources(_route("claude")), {}, ROUTE_UNVERIFIED),
    ("stale_proof", _resources(_route(checked_at=_stale())), {}, ROUTE_UNVERIFIED),
    ("no_report", _resources(None), {}, ROUTE_UNREPORTED),
    ("legacy_report", _resources({}, schema="mac.coding_clis.v1"), {}, ROUTE_UNREPORTED),
    (
        "pinned_model_unproven",
        _resources(_route(model="gpt-default")),
        {"model": "qwen-pinned"},
        MODEL_UNVERIFIED,
    ),
]


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


@pytest.fixture
def strict(cp, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT", "1")
    monkeypatch.setenv("MAC_CODING_AGENTS", "opencode")
    cp.create_project(PROJECT, dispatch_paused=False)
    return cp


def _worker(cp, resources, name="coder"):
    machine = cp.register_machine("host-" + name)
    return cp.register_agent(machine.id, name, capabilities=["python"], resources=resources)


def _task(cp, **metadata):
    return cp.create_task(
        "repo task",
        project=PROJECT,
        required_capabilities=["python"],
        metadata=_repo_metadata(**metadata),
    )


def _explained_codes(cp, task_id, agent_id):
    explained = cp.explain_task_dispatch(task_id)
    for candidate in explained["candidates"]:
        if candidate["agent_id"] == agent_id:
            return [reason["code"] for reason in candidate["reasons"]]
    raise AssertionError("agent %s not among candidates" % agent_id)


# --- dispatch_once: the allocator round ------------------------------------


@pytest.mark.parametrize("case,resources,extra,code", REFUSED, ids=[r[0] for r in REFUSED])
def test_dispatch_once_refuses_a_worker_without_a_listed_fresh_proof(
    strict, case, resources, extra, code
):
    agent = _worker(strict, resources)
    task = _task(strict, **extra)

    assert strict.dispatch_once() is None
    assert strict.get_task(task.id).state == "open"
    # The refusal is observable, with its reason, where operators look.
    assert code in _explained_codes(strict, task.id, agent.id)


def test_dispatch_once_assigns_a_worker_with_a_listed_fresh_proof(strict):
    agent = _worker(strict, _resources(_route()))
    _task(strict)

    assignment = strict.dispatch_once()

    assert assignment is not None
    assert assignment["agent"]["id"] == agent.id


def test_dispatch_once_prefers_the_proven_worker_over_an_unproven_one(strict):
    _worker(strict, _resources(_route("claude")), name="unlisted")
    proven = _worker(strict, _resources(_route()), name="proven")
    _task(strict)

    assignment = strict.dispatch_once()

    assert assignment is not None
    assert assignment["agent"]["id"] == proven.id


def test_a_second_listed_cli_is_enough(strict, monkeypatch):
    monkeypatch.setenv("MAC_CODING_AGENTS", "opencode,claude")
    agent = _worker(strict, _resources(_route("claude")))
    _task(strict)

    assignment = strict.dispatch_once()

    assert assignment is not None and assignment["agent"]["id"] == agent.id


# --- pull claim, targeted claim, retry relaxation ---------------------------


def test_pull_claim_refuses_without_proof_and_accepts_with_it(strict):
    unproven = _worker(strict, _resources(_route("claude")), name="unlisted")
    _task(strict)

    assert strict.claim_next_for_agent(unproven.id) is None

    proven = _worker(strict, _resources(_route()), name="proven")
    # An empty pull round throttles the next one for a few seconds; this
    # test is about eligibility, not that throttle.
    strict.dispatch._last_empty_pull_round_at = 0.0
    claimed = strict.claim_next_for_agent(proven.id)
    assert claimed is not None
    assert claimed["agent"]["id"] == proven.id


def test_a_task_targeted_at_an_unproven_worker_waits(strict):
    agent = _worker(strict, _resources(_route("claude")))
    task = _task(strict, target_agent_id=agent.id)

    assert strict.dispatch_once() is None
    assert strict.claim_next_for_agent(agent.id) is None
    assert strict.get_task(task.id).state == "open"
    assert ROUTE_UNVERIFIED in _explained_codes(strict, task.id, agent.id)


def test_a_task_targeted_at_a_proven_worker_is_claimed(strict):
    agent = _worker(strict, _resources(_route()))
    _task(strict, target_agent_id=agent.id)

    claimed = strict.claim_next_for_agent(agent.id)

    assert claimed is not None and claimed["agent"]["id"] == agent.id


def test_retry_relaxation_does_not_unlock_an_unproven_worker(strict):
    # The only other worker is retry-excluded. Relaxing that exclusion must
    # not route the task to a worker that cannot run it.
    excluded = _worker(strict, _resources(_route()), name="excluded")
    unproven = _worker(strict, _resources(_route("claude")), name="unlisted")
    task = _task(strict, retry_excluded_agent_ids=[excluded.id])

    assignment = strict.dispatch_once()

    assert assignment is None or assignment["agent"]["id"] == excluded.id
    assert unproven.id not in {
        candidate["agent_id"]
        for candidate in strict.explain_task_dispatch(task.id)["candidates"]
        if candidate["eligible"]
    }


# --- the transactional claim boundary ---------------------------------------


def test_a_direct_claim_is_refused_at_the_boundary(strict):
    agent = _worker(strict, _resources(_route("claude")))
    task = _task(strict)

    with pytest.raises(ValidationError, match=ROUTE_UNVERIFIED):
        strict.claim_task(task.id, agent.id)


# --- exemptions --------------------------------------------------------------


def test_non_repository_work_is_not_gated(strict):
    agent = _worker(strict, _resources(_route("claude")))
    strict.create_task(
        "answer a question",
        project=PROJECT,
        required_capabilities=["python"],
        metadata={"deliverable": "report"},
    )

    assignment = strict.dispatch_once()

    assert assignment is not None and assignment["agent"]["id"] == agent.id


def test_strict_mode_off_gates_nothing(cp, monkeypatch):
    monkeypatch.delenv("MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT", raising=False)
    cp.create_project(PROJECT, dispatch_paused=False)
    agent = _worker(cp, _resources(_route("claude")))
    _task(cp)

    assignment = cp.dispatch_once()

    assert assignment is not None and assignment["agent"]["id"] == agent.id


# --- the gate itself ---------------------------------------------------------


def test_the_default_proof_is_exempt():
    assert refusal(CodingRouteProof(), task_requires_coding=True) is None


def test_proof_ignores_unlisted_clis_and_unions_listed_models():
    env = {"MAC_OPENSHELL_REPO_REQUIRES_CODING_AGENT": "1"}
    clis = {**_route("opencode", model="a"), **_route("claude", model="b"), **_route("codex", model="c")}
    proof = proof_from_resources(
        _resources(clis), requires_openshell=True, listed_agents=["opencode", "claude"], env=env
    )
    assert proof.fresh and proof.fresh_models == frozenset({"a", "b"})
    assert refusal(proof, task_requires_coding=True, pinned_model="c") == MODEL_UNVERIFIED
    assert refusal(proof, task_requires_coding=True, pinned_model="b") is None
    assert refusal(proof, task_requires_coding=False, pinned_model="c") is None
