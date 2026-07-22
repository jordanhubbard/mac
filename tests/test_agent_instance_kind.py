from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from mac.api import create_app
from mac.cli import build_parser
from mac.models import ValidationError
from mac.services import ControlPlane


def test_agent_instance_kind_defaults_and_survives_reregistration() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("headless-host")

    static = cp.register_agent(machine.id, "rocky", agent_id="agent_rocky")
    fungible = cp.register_agent(
        machine.id,
        "worker-one",
        agent_id="agent_worker_one",
        instance_kind="fungible",
    )

    assert static.instance_kind == "static"
    assert fungible.instance_kind == "fungible"
    assert (
        cp.register_agent(
            machine.id,
            "worker-one",
            agent_id=fungible.id,
        ).instance_kind
        == "fungible"
    )


def test_agent_instance_kind_is_validated_and_exposed_in_fleet_snapshot() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("replaceable-host")
    agent = cp.register_agent(
        machine.id,
        "replaceable",
        agent_id="agent_replaceable",
        instance_kind="fungible",
    )

    member = next(
        item
        for item in cp.fleet_snapshot()["members"]
        if item["agent_id"] == agent.id
    )
    assert member["instance_kind"] == "fungible"

    assert cp.update_agent(agent.id, instance_kind="static").instance_kind == "static"
    with pytest.raises(ValidationError, match="unsupported agent instance_kind"):
        cp.update_agent(agent.id, instance_kind="temporary")
    with pytest.raises(ValidationError, match="unsupported agent instance_kind"):
        cp.register_agent(machine.id, "bad", instance_kind="dynamic")


def test_agent_instance_kind_round_trips_through_http() -> None:
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    machine = client.post("/machines", json={"hostname": "hgx-host"}).json()

    created = client.post(
        "/agents",
        json={
            "machine_id": machine["id"],
            "name": "hgx-worker",
            "instance_kind": "fungible",
        },
    )
    assert created.status_code == 200
    assert created.json()["instance_kind"] == "fungible"

    updated = client.put(
        "/agents/%s" % created.json()["id"],
        json={"instance_kind": "static"},
    )
    assert updated.status_code == 200
    assert updated.json()["instance_kind"] == "static"


def test_worker_cannot_reclassify_itself_as_fungible() -> None:
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("static-host")
    agent = cp.register_agent(machine.id, "static-worker", agent_id="agent_static")
    client = TestClient(
        create_app(
            control_plane=cp,
            auth_tokens={
                "worker": {
                    "scopes": ["agent", "read", "write"],
                    "agent_id": agent.id,
                    "principal_kind": "worker",
                }
            },
        )
    )

    response = client.post(
        "/agents",
        headers={"Authorization": "Bearer worker"},
        json={
            "machine_id": machine.id,
            "name": agent.name,
            "agent_id": agent.id,
            "instance_kind": "fungible",
        },
    )

    assert response.status_code == 403
    assert cp.get_agent(agent.id).instance_kind == "static"


def test_agent_instance_kind_is_exposed_by_cli_parser() -> None:
    register = build_parser().parse_args(
        [
            "agent",
            "register",
            "machine_hgx",
            "headless",
            "--instance-kind",
            "fungible",
        ]
    )
    assert register.instance_kind == "fungible"

    update = build_parser().parse_args(
        ["agent", "update", "agent_headless", "--instance-kind", "static"]
    )
    assert update.instance_kind == "static"
