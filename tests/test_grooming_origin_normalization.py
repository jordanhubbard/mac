"""Grooming cadence must survive the real registered-repository task path."""

from datetime import datetime, timedelta

import pytest
import yaml

from mac.backlog_groomer import BacklogGroomer, BacklogGroomerConfig
from mac.models import ValidationError
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


@pytest.fixture
def registered_project(tmp_path):
    cp = ControlPlane(ephemeral_store(), secret_key="grooming-origin-test-secret-key-32+")
    repo = tmp_path / "repo"
    (repo / ".mac").mkdir(parents=True)
    contract = {
        "schema": "mac.repository_contract.v1",
        "project": "project",
        "canonical_remote_url": "https://github.com/example/project",
        "default_branch": "main",
        "platforms": ["linux", "darwin"],
        "toolchain": {"required_commands": ["python3"]},
        "bootstrap": {"command": "python3 scripts/bootstrap.py"},
        "test": {"command": "pytest"},
        "evidence": {"required": ["tests"]},
    }
    (repo / ".mac/project.yaml").write_text(yaml.safe_dump(contract))
    cp.create_project(
        "project",
        metadata={
            "repository_url": contract["canonical_remote_url"],
            "backlog_grooming": {"enabled": True},
        },
    )
    cp.register_project_repository("registered-repository", str(repo), project="project")
    return cp, repo


def groomer(cp):
    return BacklogGroomer(
        cp, BacklogGroomerConfig(enabled=True, min_ready=2, regroom_interval_seconds=21600)
    )


def report_metadata(origin=None):
    metadata = {
        "deliverable": "report",
        "report_repository_access": {
            "schema": "mac.report_repository_access.v1",
            "mode": "read_only",
        },
    }
    if origin is not None:
        metadata["origin"] = origin
    return metadata


def test_grooming_identity_and_repository_identity_persist(registered_project):
    cp, repo = registered_project
    result = groomer(cp).run_once()
    assert result["groomed_count"] == 1
    task = cp.get_task(result["projects"][0]["task_id"])
    origin = task.metadata["origin"]
    assert origin["type"] == "backlog_grooming"
    assert origin["repository_name"] == "registered-repository"
    assert origin["repository_path"] == str(repo)
    execution = task.metadata["execution_contract"]
    assert origin["repository_id"] == execution["repository_id"]
    assert execution["source"] == "registered_project"
    assert execution["repository_contract"]["project"] == "project"


def test_inflight_groom_suppresses_duplicate_even_after_cadence(registered_project, monkeypatch):
    cp, _ = registered_project
    first = groomer(cp).run_once()
    assert first["groomed_count"] == 1
    task = cp.get_task(first["projects"][0]["task_id"])
    later = datetime.fromisoformat(task.created_at) + timedelta(hours=7)
    monkeypatch.setattr("mac.backlog_groomer._utcnow", lambda: later)
    second = groomer(cp).run_once()
    assert second["groomed_count"] == 0
    assert second["projects"][0]["skipped_reason"] == "grooming task already open"
    assert len(cp.list_tasks()) == 1


def test_failed_groom_obeys_six_hour_cadence(registered_project, monkeypatch):
    cp, _ = registered_project
    first = groomer(cp).run_once()
    assert first["groomed_count"] == 1
    task = cp.get_task(first["projects"][0]["task_id"])
    cp._transition_task_internal(
        task.id, "failed", actor="test", detail={"reason": "executor failed"}
    )
    created = datetime.fromisoformat(task.created_at)
    monkeypatch.setattr("mac.backlog_groomer._utcnow", lambda: created + timedelta(minutes=15))
    second = groomer(cp).run_once()
    assert second["groomed_count"] == 0
    assert second["projects"][0]["skipped_reason"] == "groomed 900s ago (< 21600s)"
    monkeypatch.setattr("mac.backlog_groomer._utcnow", lambda: created + timedelta(hours=6))
    third = groomer(cp).run_once()
    assert third["groomed_count"] == 1
    assert third["projects"][0]["task_id"] != task.id


@pytest.mark.parametrize("origin", [None, {"type": "project_onboarding"}])
def test_ordinary_report_does_not_become_a_groom(registered_project, origin):
    cp, _ = registered_project
    task = cp.create_task("Ordinary report", project="project", metadata=report_metadata(origin))
    expected = "direct_task" if origin is None else origin["type"]
    assert cp.get_task(task.id).metadata["origin"]["type"] == expected
    cp._transition_task_internal(task.id, "failed", actor="test")
    assert groomer(cp).run_once()["groomed_count"] == 1


@pytest.mark.parametrize("field,value", [("repository_id", "wrong"), ("repository_path", "/wrong")])
def test_preserved_producer_cannot_override_registered_repository(registered_project, field, value):
    cp, _ = registered_project
    with pytest.raises(ValidationError, match="contradicts the current registered repository"):
        cp.create_task(
            "Groom backlog",
            project="project",
            metadata=report_metadata({"type": "backlog_grooming", field: value}),
        )
