"""Report-task origin must be normalized to the registered repository identity."""

import pytest
import yaml

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
        metadata={"repository_url": contract["canonical_remote_url"]},
    )
    cp.register_project_repository("registered-repository", str(repo), project="project")
    return cp, repo


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


def test_producer_origin_gets_registered_repository_identity(registered_project):
    cp, repo = registered_project
    task = cp.create_task(
        "Report", project="project", metadata=report_metadata({"type": "scientific_optimizer"})
    )
    origin = cp.get_task(task.id).metadata["origin"]
    assert origin["type"] == "scientific_optimizer"
    assert origin["repository_name"] == "registered-repository"
    assert origin["repository_path"] == str(repo)
    execution = cp.get_task(task.id).metadata["execution_contract"]
    assert origin["repository_id"] == execution["repository_id"]
    assert execution["source"] == "registered_project"
    assert execution["repository_contract"]["project"] == "project"


@pytest.mark.parametrize("origin", [None, {"type": "project_onboarding"}])
def test_ordinary_report_keeps_its_origin_type(registered_project, origin):
    cp, _ = registered_project
    task = cp.create_task("Ordinary report", project="project", metadata=report_metadata(origin))
    expected = "direct_task" if origin is None else origin["type"]
    assert cp.get_task(task.id).metadata["origin"]["type"] == expected


@pytest.mark.parametrize("field,value", [("repository_id", "wrong"), ("repository_path", "/wrong")])
def test_preserved_producer_cannot_override_registered_repository(registered_project, field, value):
    cp, _ = registered_project
    with pytest.raises(ValidationError, match="contradicts the current registered repository"):
        cp.create_task(
            "Report",
            project="project",
            metadata=report_metadata({"type": "scientific_optimizer", field: value}),
        )
