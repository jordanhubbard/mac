from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

import mac.cli as cli
from mac.api import create_app
from mac.dispatch import DispatchError, LocalDispatch, RemoteDispatch
from mac.services import ControlPlane
from mac.store import SQLiteStore
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_service import RepositoryBaseAttestation


BASE_SHA = "a" * 40


class _Verifier:
    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=planning_base_sha,
            source_kind="test",
            verified_at="attested",
            resource_namespace={"status": "unresolved"},
        )


def _plan(*, generation: int) -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_surface",
        "goal": "Exercise every replan control surface",
        "project": "mac",
        "repository_id": "repo_surface",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": BASE_SHA,
        "plan_generation": generation,
        "nodes": [
            {
                "node_key": "change",
                "title": "Change source",
                "description": "generation %d" % generation,
                "node_type": "mutation",
                "effects": {"writes": ["src"]},
                "expected_outputs": ["candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            }
        ],
    }


def _control_plane() -> tuple[SQLiteStore, ControlPlane]:
    store = SQLiteStore(":memory:")
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "repo_surface",
            "surface",
            "/controller/surface",
            "ssh://git@example.invalid/surface.git",
            "mac",
            "[]",
            1,
            60,
            "{}",
            "created",
            "registry-v1",
        ),
    )
    control = ControlPlane(store, secret_key="work-package-replan-surface-key-0001")
    control.work_packages.repository_verifier = _Verifier()
    control.work_package_replans.repository_verifier = _Verifier()
    return store, control


def test_admin_api_exposes_preview_pause_and_atomic_replan() -> None:
    store, control = _control_plane()
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
        admitted = client.post(
            "/work-packages",
            headers=admin,
            json={"plan": _plan(generation=1), "reason": "initial"},
        )
        assert admitted.status_code == 200, admitted.text

        request = {
            "plan": _plan(generation=2),
            "expected_plan_version": 1,
            "expected_epoch": 1,
            "actor": "replan-controller",
            "reason": "correct the DAG",
        }
        denied = client.post(
            "/work-packages/wp_surface/replan-preview",
            headers=write,
            json=request,
        )
        assert denied.status_code == 403

        active_preview = client.post(
            "/work-packages/wp_surface/replan-preview",
            headers=admin,
            json=request,
        )
        assert active_preview.status_code == 200, active_preview.text
        assert active_preview.json()["preview"]["can_apply"] is False
        assert "must be paused" in " ".join(
            active_preview.json()["preview"]["blockers"]
        )

        pause = client.post(
            "/work-packages/wp_surface/pause",
            headers=admin,
            json={
                "expected_plan_version": 1,
                "expected_epoch": 1,
                "actor": "operator",
                "reason": "raise Andon",
            },
        )
        assert pause.status_code == 200, pause.text
        assert pause.json()["state"] == "paused"
        assert pause.json()["changed"] is True

        paused_preview = client.post(
            "/work-packages/wp_surface/replan-preview",
            headers=admin,
            json=request,
        )
        assert paused_preview.status_code == 200, paused_preview.text
        assert paused_preview.json()["preview"]["can_apply"] is True
        assert paused_preview.json()["proposal"]["proposed_epoch"] == 2

        replanned = client.post(
            "/work-packages/wp_surface/replan",
            headers=admin,
            json=request,
        )
        assert replanned.status_code == 200, replanned.text
        assert replanned.json()["result"]["created"] is True
        assert replanned.json()["result"]["plan_version"] == 2
        assert replanned.json()["result"]["epoch"] == 2
        described = client.get("/work-packages/wp_surface", headers=admin)
        assert described.json()["package"]["state"] == "paused"
        assert described.json()["package"]["current_plan_version"] == 2
        assert described.json()["package"]["current_epoch"] == 2
    finally:
        store.close()


class _RecordingClient:
    def __init__(self) -> None:
        self.calls = []

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        return {"ok": True}


def test_remote_dispatch_serializes_all_replan_operations() -> None:
    client = _RecordingClient()
    dispatch = RemoteDispatch(client)
    plan = _plan(generation=2)

    dispatch.preview_work_package_replan(
        "wp/slash",
        plan,
        expected_plan_version=1,
        expected_epoch=2,
        actor="planner",
        reason="preview",
    )
    dispatch.pause_work_package(
        "wp/slash",
        expected_plan_version=1,
        expected_epoch=2,
        actor="operator",
        reason="Andon",
    )
    dispatch.replan_work_package(
        "wp/slash",
        plan,
        expected_plan_version=1,
        expected_epoch=2,
        actor="planner",
        reason="apply",
    )

    common = {
        "expected_plan_version": 1,
        "expected_epoch": 2,
        "actor": "planner",
    }
    assert client.calls == [
        (
            "POST",
            "/work-packages/wp%2Fslash/replan-preview",
            {**common, "plan": plan, "reason": "preview"},
        ),
        (
            "POST",
            "/work-packages/wp%2Fslash/pause",
            {
                "expected_plan_version": 1,
                "expected_epoch": 2,
                "actor": "operator",
                "reason": "Andon",
            },
        ),
        (
            "POST",
            "/work-packages/wp%2Fslash/replan",
            {**common, "plan": plan, "reason": "apply"},
        ),
    ]


class _LocalPlane:
    def __init__(self) -> None:
        self.calls = []

    def replan_work_package(self, package_id, plan, **kwargs):
        self.calls.append((package_id, plan, kwargs))
        return {"created": True}


def test_local_dispatch_guards_replan_as_task_producing() -> None:
    plane = _LocalPlane()
    blocked = LocalDispatch(
        plane,
        db_path="/tmp/replica.db",
        local_authority_confirmed=False,
        remote_authority="http://hub:8789",
    )
    with pytest.raises(DispatchError, match="authoritative|hub"):
        blocked.replan_work_package(
            "wp_surface",
            _plan(generation=2),
            expected_plan_version=1,
            expected_epoch=1,
            actor="planner",
            reason="apply",
        )
    assert plane.calls == []

    allowed = LocalDispatch(plane, local_authority_confirmed=True)
    allowed.replan_work_package(
        "wp_surface",
        _plan(generation=2),
        expected_plan_version=1,
        expected_epoch=1,
        actor="planner",
        reason="apply",
    )
    assert len(plane.calls) == 1


class _CliPlane:
    def __init__(self) -> None:
        self.calls = []

    def preview_work_package_replan(self, package_id, plan, **kwargs):
        self.calls.append(("preview", package_id, plan, kwargs))
        return {"preview": {"can_apply": True}}

    def pause_work_package(self, package_id, **kwargs):
        self.calls.append(("pause", package_id, None, kwargs))
        return {"state": "paused"}

    def replan_work_package(self, package_id, plan, **kwargs):
        self.calls.append(("replan", package_id, plan, kwargs))
        return {"result": {"created": True}}


def test_cli_registers_and_routes_replan_preview_pause_and_replan(monkeypatch) -> None:
    plane = _CliPlane()
    outputs = []
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "_print", outputs.append)
    plan_json = json.dumps(_plan(generation=2))
    parser = cli.build_parser()

    preview = parser.parse_args(
        [
            "work-package",
            "replan-preview",
            "wp_surface",
            "--plan",
            plan_json,
            "--plan-version",
            "1",
            "--epoch",
            "1",
            "--reason",
            "preview",
            "--actor",
            "planner",
        ]
    )
    preview.func(preview)
    pause = parser.parse_args(
        [
            "work-package",
            "pause",
            "wp_surface",
            "--plan-version",
            "1",
            "--epoch",
            "1",
            "--reason",
            "Andon",
            "--actor",
            "operator",
        ]
    )
    pause.func(pause)
    replan = parser.parse_args(
        [
            "work-package",
            "replan",
            "wp_surface",
            "--plan",
            plan_json,
            "--plan-version",
            "1",
            "--epoch",
            "1",
            "--reason",
            "apply",
            "--actor",
            "planner",
        ]
    )
    replan.func(replan)

    assert plane.calls == [
        (
            "preview",
            "wp_surface",
            _plan(generation=2),
            {
                "expected_plan_version": 1,
                "expected_epoch": 1,
                "actor": "planner",
                "reason": "preview",
            },
        ),
        (
            "pause",
            "wp_surface",
            None,
            {
                "expected_plan_version": 1,
                "expected_epoch": 1,
                "actor": "operator",
                "reason": "Andon",
            },
        ),
        (
            "replan",
            "wp_surface",
            _plan(generation=2),
            {
                "expected_plan_version": 1,
                "expected_epoch": 1,
                "actor": "planner",
                "reason": "apply",
            },
        ),
    ]
    assert len(outputs) == 3
