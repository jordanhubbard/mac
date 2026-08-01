from __future__ import annotations

from fastapi.testclient import TestClient

import mac.cli as cli
from mac.api import create_app
from mac.dispatch import DispatchError, LocalDispatch, RemoteDispatch
from mac.services import ControlPlane
from mac.store import Store
from mac.test_support import ephemeral_store
from mac.work_package_integration_service import (
    IntegrationAssemblyOutcome,
    IntegrationBatchCreation,
    IntegrationLease,
)


class _IntegrationStation:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def create_batch(self, package_id: str, node_key: str, *, actor: str):
        self.calls.append(("create", package_id, node_key, actor))
        return IntegrationBatchCreation(
            batch_id="wpbatch_surface",
            package_id=package_id,
            plan_version=3,
            epoch=4,
            integration_node_key=node_key,
            landing_base_sha="a" * 40,
            input_digest="sha256:" + "b" * 64,
            input_ids=("wpinput_a", "wpinput_b"),
            created=True,
        )

    def status(self, batch_id: str):
        self.calls.append(("status", batch_id))
        return {
            "schema": "mac.work_package.integration_batch.v1",
            "batch_id": batch_id,
            "state": "assembling",
            "lease_fence": 7,
        }

    def claim(self, batch_id: str):
        self.calls.append(("claim", batch_id))
        return IntegrationLease(
            batch_id=batch_id,
            owner="integration-surface",
            fence=7,
            expires_at="2026-07-17T12:02:00+00:00",
        )

    def assemble(self, batch_id: str):
        self.calls.append(("assemble", batch_id))
        return IntegrationAssemblyOutcome(
            status="assembled",
            batch_id=batch_id,
            candidate_sha="c" * 40,
            candidate_tree_digest="git-tree:" + "d" * 40,
            candidate_ref="refs/mac/integration/wp_surface/%s" % batch_id,
            input_digest="sha256:" + "b" * 64,
            fence=7,
        )


def _control_plane() -> tuple[Store, ControlPlane, _IntegrationStation]:
    store = ephemeral_store()
    control = ControlPlane(
        store,
        secret_key="work-package-integration-surface-key-0001",
    )
    station = _IntegrationStation()
    control.work_package_integrations = station
    return store, control, station


def test_control_plane_and_admin_api_expose_fenced_assembly_controls() -> None:
    store, control, station = _control_plane()
    try:
        client = TestClient(
            create_app(
                control_plane=control,
                auth_tokens={
                    "admin-token": {"scopes": ["admin"]},
                    "write-token": {"scopes": ["write"]},
                },
            )
        )
        admin = {"Authorization": "Bearer admin-token"}
        write = {"Authorization": "Bearer write-token"}
        request = {
            "integration_node_key": "assemble",
            "actor": "integration-controller",
        }

        denied = client.post(
            "/work-packages/wp_surface/assemble", headers=write, json=request
        )
        assert denied.status_code == 403

        created = client.post(
            "/work-packages/wp_surface/integration-batches",
            headers=admin,
            json=request,
        )
        assert created.status_code == 200, created.text
        assert created.json()["batch_id"] == "wpbatch_surface"

        status = client.get(
            "/work-package-integration-batches/wpbatch_surface", headers=admin
        )
        assert status.status_code == 200, status.text
        assert status.json()["lease_fence"] == 7

        claimed = client.post(
            "/work-package-integration-batches/wpbatch_surface/claim",
            headers=admin,
        )
        assert claimed.status_code == 200, claimed.text
        assert claimed.json()["owner"] == "integration-surface"

        assembled_batch = client.post(
            "/work-package-integration-batches/wpbatch_surface/assemble",
            headers=admin,
        )
        assert assembled_batch.status_code == 200, assembled_batch.text
        assert assembled_batch.json()["status"] == "assembled"

        assembled = client.post(
            "/work-packages/wp_surface/assemble", headers=admin, json=request
        )
        assert assembled.status_code == 200, assembled.text
        assert assembled.json()["batch"]["batch_id"] == "wpbatch_surface"
        assert assembled.json()["assembly"]["candidate_sha"] == "c" * 40
        assert station.calls == [
            ("create", "wp_surface", "assemble", "integration-controller"),
            ("status", "wpbatch_surface"),
            ("claim", "wpbatch_surface"),
            ("assemble", "wpbatch_surface"),
            ("create", "wp_surface", "assemble", "integration-controller"),
            ("assemble", "wpbatch_surface"),
        ]
    finally:
        store.close()


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def request(self, method: str, path: str, body=None):
        self.calls.append((method, path, body))
        return {"ok": True}


def test_remote_dispatch_serializes_every_integration_station_operation() -> None:
    client = _RecordingClient()
    dispatch = RemoteDispatch(client)
    dispatch.create_work_package_integration_batch(
        "wp/slash", "assemble", actor="controller"
    )
    dispatch.work_package_integration_status("batch/slash")
    dispatch.claim_work_package_integration_batch("batch/slash")
    dispatch.assemble_work_package_integration_batch("batch/slash")
    dispatch.assemble_work_package("wp/slash", "assemble", actor="controller")

    assert client.calls == [
        (
            "POST",
            "/work-packages/wp%2Fslash/integration-batches",
            {"integration_node_key": "assemble", "actor": "controller"},
        ),
        ("GET", "/work-package-integration-batches/batch%2Fslash", None),
        (
            "POST",
            "/work-package-integration-batches/batch%2Fslash/claim",
            {},
        ),
        (
            "POST",
            "/work-package-integration-batches/batch%2Fslash/assemble",
            {},
        ),
        (
            "POST",
            "/work-packages/wp%2Fslash/assemble",
            {"integration_node_key": "assemble", "actor": "controller"},
        ),
    ]


class _LocalPlane:
    def assemble_work_package(self, package_id, integration_node_key, *, actor):
        return {"package_id": package_id, "integration_node_key": integration_node_key}


def test_local_replica_cannot_start_integration_assembly() -> None:
    blocked = LocalDispatch(
        _LocalPlane(),
        db_path="/tmp/replica.db",
        local_authority_confirmed=False,
        remote_authority="http://hub:8789",
    )
    try:
        blocked.assemble_work_package(
            "wp_surface", "assemble", actor="integration-controller"
        )
    except DispatchError as exc:
        assert "authoritative" in str(exc) or "hub" in str(exc)
    else:
        raise AssertionError("replica assembly was not authority-gated")


class _CliPlane:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def assemble_work_package(self, package_id, node_key, *, actor):
        self.calls.append(("assemble", package_id, node_key, actor))
        return {"assembly": {"status": "assembled"}}

    def work_package_integration_status(self, batch_id):
        self.calls.append(("status", batch_id))
        return {"batch_id": batch_id, "state": "assembling"}

    def claim_work_package_integration_batch(self, batch_id):
        self.calls.append(("claim", batch_id))
        return {"batch_id": batch_id, "fence": 2}

    def assemble_work_package_integration_batch(self, batch_id):
        self.calls.append(("assemble-batch", batch_id))
        return {"batch_id": batch_id, "status": "assembled"}


def test_cli_routes_assembly_and_explicit_batch_controls(monkeypatch) -> None:
    plane = _CliPlane()
    outputs = []
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_print", outputs.append)
    parser = cli.build_parser()
    commands = [
        [
            "work-package",
            "assemble",
            "wp_surface",
            "assemble",
            "--actor",
            "controller",
        ],
        ["work-package", "assembly-status", "wpbatch_surface"],
        ["work-package", "assembly-claim", "wpbatch_surface"],
        ["work-package", "assemble-batch", "wpbatch_surface"],
    ]
    for command in commands:
        args = parser.parse_args(command)
        args.func(args)

    assert plane.calls == [
        ("assemble", "wp_surface", "assemble", "controller"),
        ("status", "wpbatch_surface"),
        ("claim", "wpbatch_surface"),
        ("assemble-batch", "wpbatch_surface"),
    ]
    assert len(outputs) == 4
