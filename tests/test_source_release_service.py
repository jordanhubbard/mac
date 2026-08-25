from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mac.api import TokenPrincipal, create_app
from mac.models import ValidationError
from mac.services import ControlPlane


SHA = "1" * 40
DIGEST = "sha256:" + "2" * 64


def _release(cp: ControlPlane, **overrides):
    values = {
        "repository_id": "projectrepo_mac",
        "repository_name": "mac",
        "canonical_remote_url": "git@github.com:example/mac.git",
        "commit_sha": SHA,
        "canonical_ref": SHA,
        "tree_digest": DIGEST,
        "created_by": "human_alice",
        "metadata": {"branch": "main", "convergence_action": "source_restart"},
    }
    values.update(overrides)
    return cp.register_source_release(**values)


def test_release_registration_is_idempotent_and_immutable():
    cp = ControlPlane.in_memory()

    first = _release(cp)
    second = _release(cp)

    assert second.id == first.id
    with pytest.raises(ValidationError, match="different immutable material"):
        _release(cp, tree_digest="sha256:" + "3" * 64)


def test_published_release_requires_green_ci_and_local_contract_tests():
    cp = ControlPlane.in_memory()

    with pytest.raises(ValidationError, match="review and publication evidence"):
        _release(cp, status="published")


def test_desired_source_is_monotonic_and_request_idempotent():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("worker-host")
    agent = cp.register_agent(machine.id, "worker")
    fleet = cp.create_fleet("primary", agent_ids=[agent.id])
    release = _release(cp)
    cp.store.execute("UPDATE source_releases SET status = 'published' WHERE id = ?", (release.id,))

    first = cp.set_fleet_desired_source(
        fleet_id=fleet.id,
        release_id=release.id,
        actor="human_alice",
        reason="approved current",
        request_id="upgrade-1",
    )
    retry = cp.set_fleet_desired_source(
        fleet_id=fleet.id,
        release_id=release.id,
        actor="human_alice",
        reason="approved current",
        request_id="upgrade-1",
    )
    second = cp.set_fleet_desired_source(
        fleet_id=fleet.id,
        release_id=release.id,
        actor="human_alice",
        reason="approved current again",
        request_id="upgrade-2",
        expected_generation=1,
    )

    assert retry.id == first.id
    assert retry.generation == 1
    assert second.id == first.id
    assert second.generation == 2
    assert second.prior_generation == 1


def test_release_write_api_requires_deploy_scope_and_uses_principal_identity():
    cp = ControlPlane.in_memory()
    human = cp.register_human(username="alice")
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "deploy": TokenPrincipal(scopes=frozenset({"deploy"}), human_id=human.id),
            "writer": TokenPrincipal(scopes=frozenset({"write"}), human_id=human.id),
        },
    )
    payload = {
        "repository_id": "projectrepo_mac",
        "repository_name": "mac",
        "canonical_remote_url": "git@github.com:example/mac.git",
        "commit_sha": SHA,
        "canonical_ref": SHA,
        "tree_digest": DIGEST,
    }

    denied = TestClient(app).post(
        "/source-releases",
        headers={"Authorization": "Bearer writer"},
        json=payload,
    )
    accepted = TestClient(app).post(
        "/source-releases",
        headers={"Authorization": "Bearer deploy"},
        json=payload,
    )

    assert denied.status_code == 403
    assert accepted.status_code in {200, 201}, accepted.text
    assert accepted.json()["created_by"] == human.id
