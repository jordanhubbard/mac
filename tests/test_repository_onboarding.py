"""Tests for Git-backed project registration (ControlPlane.register_project).

Onboarding takes a git URL and creates a single read-only task whose
``metadata.origin`` is shaped so a worker clones a task-owned worktree WITHOUT a
pre-existing repository_contract — the contract is the onboarding task's output,
not a precondition. The decisive assertion is the cross-module one:
``worker._repository_task_origin`` must classify the created task as a
repository task, otherwise no worktree would be prepared.
"""

from __future__ import annotations

import pytest

from mac.models import ValidationError
from mac.services import (
    ControlPlane,
    _normalize_onboarding_remote_url,
    _normalize_repository_registration,
    _repository_name_from_url,
)
from mac.worker import _repository_task_origin


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def test_repository_name_from_url():
    assert _repository_name_from_url("https://github.com/NVIDIA-dev/taskbrain.git") == "taskbrain"
    assert _repository_name_from_url("git@github.com:NVIDIA-dev/taskbrain.git") == "taskbrain"
    assert _repository_name_from_url("https://example.com/group/sub/widget/") == "widget"
    assert _repository_name_from_url("https://example.com/Repo.Name.git") == "Repo.Name"


def test_normalize_onboarding_remote_url_rejects_junk():
    for bad in ["", "   ", "-oProxyCommand=evil", "not-a-url", "ftp://x/y"]:
        with pytest.raises(ValidationError):
            _normalize_onboarding_remote_url(bad)
    assert _normalize_onboarding_remote_url("https://github.com/o/r.git") == "https://github.com/o/r.git"


def test_repository_registration_uses_url_fragment_and_defaults_to_main():
    assert _normalize_repository_registration(
        "https://github.com/o/r.git"
    ) == (
        "https://github.com/o/r.git",
        "main",
        "https://github.com/o/r.git#main",
    )
    assert _normalize_repository_registration(
        "git@github.com:o/r.git#feature/one"
    ) == (
        "git@github.com:o/r.git",
        "feature/one",
        "git@github.com:o/r.git#feature/one",
    )
    with pytest.raises(ValidationError, match="conflicts"):
        _normalize_repository_registration(
            "https://github.com/o/r.git#develop",
            default_branch="main",
        )


def test_register_project_shapes_origin_for_worktree(cp):
    task = cp.register_project("https://github.com/NVIDIA-dev/taskbrain.git")
    origin = task.metadata["origin"]
    assert origin["type"] == "direct_task"
    assert origin["repository_url"] == "https://github.com/NVIDIA-dev/taskbrain.git"
    assert origin["repository_registration"] == (
        "https://github.com/NVIDIA-dev/taskbrain.git#main"
    )
    assert origin["default_branch"] == "main"
    assert origin["repository_name"] == "taskbrain"
    assert origin["onboarding"] is True
    # Project defaults to the repo name; evidence is an investigation write-up.
    assert task.project == "taskbrain"
    assert task.metadata.get("evidence_type") == "investigation"
    # The decisive cross-module contract: the worker recognizes this as a
    # repository task (so it will clone a worktree) even with no contract.
    detected = _repository_task_origin({"metadata": task.metadata})
    assert detected is not None
    assert detected["repository_url"] == "https://github.com/NVIDIA-dev/taskbrain.git"


def test_register_project_honors_overrides(cp):
    task = cp.register_project(
        "https://github.com/o/widget.git",
        project="custom-proj",
        default_branch="develop",
        title="Custom onboarding title",
    )
    assert task.project == "custom-proj"
    assert task.title == "Custom onboarding title"
    assert task.metadata["origin"]["default_branch"] == "develop"


def test_register_project_description_directs_contract_authoring(cp):
    task = cp.register_project("https://github.com/o/widget.git")
    assert ".mac/project.yaml" in task.description
    assert "$MAC_TASK_REPO_WORKTREE" in task.description
    assert "codegraph init" in task.description
    assert "do NOT push" in task.description


def test_register_project_description_reads_repo_self_description(cp):
    # Sane-defaults onboarding must point the worker at the repo's own
    # self-describing files, not just guess from code.
    task = cp.register_project("https://github.com/o/widget.git")
    for self_doc in ("README.md", "AGENTS.md", "PLAN.md"):
        assert self_doc in task.description


# -- Gap B: onboarding registers a first-class project record ----------------


def test_register_project_creates_project_record(cp):
    cp.register_project("https://github.com/o/widget.git")
    record = cp.get_project_record("widget")  # would raise NotFoundError pre-fix
    assert record.metadata.get("repository_url") == "https://github.com/o/widget.git"
    # get_project's summary must expose the repo association first-class and the
    # record must be non-null (it was None when onboarding made only a task).
    summary = cp.get_project("widget")
    assert summary["record"] is not None
    assert summary["summary"]["repository_url"] == "https://github.com/o/widget.git"


def test_register_project_record_is_active_so_task_dispatches(cp):
    # The single onboarding task must be allowed to run; a paused project would
    # hold it forever. Active is the whole point of onboarding.
    cp.register_project("https://github.com/o/widget.git")
    assert cp._project_dispatch_paused("widget") is False


def test_register_project_is_idempotent(cp):
    cp.register_project("https://github.com/o/widget.git")
    cp.register_project("https://github.com/o/widget.git")
    records = [r for r in cp.list_project_records() if r.name == "widget"]
    assert len(records) == 1  # no duplicate project
    assert records[0].metadata.get("repository_url") == "https://github.com/o/widget.git"


def test_register_project_records_default_branch(cp):
    cp.register_project("https://github.com/o/widget.git", default_branch="develop")
    record = cp.get_project_record("widget@develop")
    assert record.metadata.get("default_branch") == "develop"
    assert record.metadata.get("repository_registration") == (
        "https://github.com/o/widget.git#develop"
    )


def test_branch_qualified_registrations_are_distinct_internal_projects(cp):
    cp.register_project(
        "https://github.com/o/widget.git#main",
        project="widget-main",
    )
    cp.register_project(
        "https://github.com/o/widget.git#feature/one",
        project="widget-feature",
    )
    assert cp.get_project_record("widget-main").metadata[
        "repository_registration"
    ].endswith("#main")
    assert cp.get_project_record("widget-feature").metadata[
        "repository_registration"
    ].endswith("#feature/one")


def test_duplicate_url_and_branch_registration_is_rejected(cp):
    cp.register_project(
        "https://github.com/o/widget.git#main",
        project="widget-one",
    )
    with pytest.raises(ValidationError, match="already owned by project widget-one"):
        cp.register_project(
            "https://github.com/o/widget.git",
            project="widget-two",
        )


def test_project_update_changes_working_branch_and_canonical_registration(cp):
    cp.register_project(
        "https://github.com/o/widget.git",
        project="widget",
    )
    updated = cp.update_project("widget", default_branch="release/next")
    assert updated.metadata["repository_url"] == "https://github.com/o/widget.git"
    assert updated.metadata["default_branch"] == "release/next"
    assert updated.metadata["repository_registration"] == (
        "https://github.com/o/widget.git#release/next"
    )


def test_project_update_propagates_branch_to_internal_checkout_contract(cp, tmp_path):
    repo = tmp_path / "widget"
    contract_dir = repo / ".mac"
    contract_dir.mkdir(parents=True)
    (contract_dir / "project.yaml").write_text(
        "\n".join(
            [
                "schema: mac.repository_contract.v1",
                "project: widget",
                "canonical_remote_url: https://github.com/o/widget.git",
                "default_branch: main",
                "platforms: [linux]",
                "toolchain:",
                "  required_commands: [python3]",
                "bootstrap:",
                "  command: python3 scripts/bootstrap.py",
                "  creates: [.venv/bin/python]",
                "test:",
                "  command: pytest",
                "evidence:",
                "  required: [tests]",
                "",
            ]
        ),
        encoding="utf-8",
    )
    cp.register_project(
        "https://github.com/o/widget.git",
        project="widget",
    )
    cp.register_project_repository(
        "widget",
        str(repo),
        project="widget",
    )

    cp.update_project("widget", default_branch="release/next")

    [registered] = cp.list_project_repositories()
    contract = registered.metadata["repository_contract"]
    assert contract["default_branch"] == "release/next"
    assert contract["canonical_remote_url"] == "https://github.com/o/widget.git"


def test_register_project_preserves_existing_paused_state(cp):
    # A pre-existing project (operator may have paused it) must keep its
    # dispatch state; onboarding only fills in the missing repo URL.
    cp.create_project("widget", dispatch_paused=True)
    cp.register_project("https://github.com/o/widget.git", project="widget")
    assert cp._project_dispatch_paused("widget") is True
    assert (
        cp.get_project_record("widget").metadata.get("repository_url")
        == "https://github.com/o/widget.git"
    )


def test_repo_project_without_registered_contract_rejects_normal_tasks(cp):
    cp.create_project(
        "widget",
        metadata={"repository_url": "https://github.com/o/widget.git"},
        dispatch_paused=False,
    )

    with pytest.raises(ValidationError, match="no registered repository contract"):
        cp.create_task("Fix widget bug", project="widget", required_capabilities=["python"])


def test_onboarding_task_allowed_before_registered_contract(cp):
    task = cp.register_project("https://github.com/o/widget.git", project="widget")

    assert task.project == "widget"
    assert task.metadata["origin"]["onboarding"] is True
    assert task.metadata["execution_contract"]["type"] == "operator_directive"
    assert task.metadata["execution_contract"]["evidence_type"] == "investigation"


# -- Gap A: `project create` rejects git-URL-shaped names --------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/NVIDIA-dev/ova.git",
        "git@github.com:NVIDIA-dev/ova.git",
        "ssh://git@github.com/NVIDIA-dev/ova.git",
        "git://github.com/NVIDIA-dev/ova.git",
    ],
)
def test_create_project_rejects_url_shaped_name(cp, url):
    with pytest.raises(ValidationError) as exc:
        cp.create_project(url)
    # Error must steer the caller to the registration path, not just fail.
    assert "project register" in str(exc.value)
    # And no junk project named after the URL should exist.
    assert not [r for r in cp.list_project_records() if r.name == url]


def test_create_project_allows_normal_name(cp):
    assert cp.create_project("ova").name == "ova"
