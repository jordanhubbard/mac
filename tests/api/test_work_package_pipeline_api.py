from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.services import ControlPlane


def _app() -> object:
    return create_app(
        control_plane=ControlPlane.in_memory(),
        auth_tokens={
            "reader": ["read"],
            "admin": ["admin"],
        },
    )


def test_pipeline_status_is_readable_but_trigger_is_global_admin(monkeypatch) -> None:
    monkeypatch.delenv("MAC_WORK_PACKAGE_PIPELINE_ENABLED", raising=False)
    app = _app()
    with TestClient(app) as client:
        status = client.get(
            "/work-package-pipeline/status",
            headers={"Authorization": "Bearer reader"},
        )
        assert status.status_code == 200
        assert status.json()["runtime"]["enabled"] is False
        assert status.json()["thread_alive"] is False

        refused = client.post(
            "/work-package-pipeline/trigger",
            headers={"Authorization": "Bearer reader"},
        )
        assert refused.status_code == 403
        disabled = client.post(
            "/work-package-pipeline/trigger",
            headers={"Authorization": "Bearer admin"},
        )
        assert disabled.status_code == 200
        assert disabled.json()["accepted"] is False


def test_enabled_pipeline_follows_app_lifecycle_and_trigger_is_wake_only(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("MAC_WORK_PACKAGE_PIPELINE_ENABLED", "true")
    monkeypatch.setenv("MAC_WORK_PACKAGE_LANDING_ENABLED", "true")
    monkeypatch.setenv("MAC_WORK_PACKAGE_BUNDLE_DIR", str(tmp_path / "bundles"))
    monkeypatch.setenv("MAC_WORK_PACKAGE_PIPELINE_INITIAL_DELAY_SECONDS", "999")
    monkeypatch.setenv("MAC_WORK_PACKAGE_PIPELINE_INTERVAL_SECONDS", "999")
    app = _app()
    pipeline = app.state.work_package_pipeline
    assert pipeline.status()["thread_alive"] is False
    assert not (tmp_path / "bundles").exists()

    with TestClient(app) as client:
        assert pipeline.status()["thread_alive"] is True
        response = client.post(
            "/work-package-pipeline/trigger",
            headers={"Authorization": "Bearer admin"},
        )
        assert response.status_code == 200
        assert response.json()["accepted"] is True
        # Trigger does not perform Git or OpenShell work inline and an empty
        # inventory does not create the rebuildable bundle cache.
        assert not (tmp_path / "bundles").exists()

    assert pipeline.status()["thread_alive"] is False


def test_execution_cohort_export_is_readable_and_outcome_links_are_admin_only() -> None:
    cp = ControlPlane.in_memory()
    task = cp.create_task("Telemetry API control task")
    app = create_app(
        control_plane=cp,
        auth_tokens={"reader": ["read"], "admin": ["admin"]},
    )
    with TestClient(app) as client:
        response = client.get(
            "/work-package-telemetry",
            params={"treatment_route": "legacy_async", "limit": 100},
            headers={"Authorization": "Bearer reader"},
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["schema"] == "mac.work_package.telemetry_export.v1"
        assignment = next(
            row for row in payload["cohort_assignments"] if row["task_id"] == task.id
        )
        assert assignment["treatment_route"] == "legacy_async"
        assert assignment["eligibility"] == "ineligible"
        assert payload["measurement_health"]["failure_count"] == 0

        comparable = client.get(
            "/work-package-telemetry/comparable-atomic-outcomes",
            headers={"Authorization": "Bearer reader"},
        )
        assert comparable.status_code == 200
        assert comparable.json() == []

        refused = client.post(
            "/work-package-finalizations/finalization_missing/outcomes",
            json={
                "outcome_type": "incident",
                "external_id": "incident-1",
                "observed_at": "2026-07-17T00:00:00+00:00",
            },
            headers={"Authorization": "Bearer reader"},
        )
        assert refused.status_code == 403
