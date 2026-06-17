"""Tests for repository onboarding (ControlPlane.onboard_repository).

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


def test_onboard_repository_shapes_origin_for_worktree(cp):
    task = cp.onboard_repository("https://github.com/NVIDIA-dev/taskbrain.git")
    origin = task.metadata["origin"]
    assert origin["type"] == "direct_task"
    assert origin["repository_url"] == "https://github.com/NVIDIA-dev/taskbrain.git"
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


def test_onboard_repository_honors_overrides(cp):
    task = cp.onboard_repository(
        "https://github.com/o/widget.git",
        project="custom-proj",
        default_branch="develop",
        title="Custom onboarding title",
    )
    assert task.project == "custom-proj"
    assert task.title == "Custom onboarding title"
    assert task.metadata["origin"]["default_branch"] == "develop"


def test_onboard_repository_description_directs_contract_authoring(cp):
    task = cp.onboard_repository("https://github.com/o/widget.git")
    assert ".mac/project.yaml" in task.description
    assert "$MAC_TASK_REPO_WORKTREE" in task.description
    assert "do NOT push" in task.description


def test_onboard_repository_description_reads_repo_self_description(cp):
    # Sane-defaults onboarding must point the worker at the repo's own
    # self-describing files, not just guess from code.
    task = cp.onboard_repository("https://github.com/o/widget.git")
    for self_doc in ("README.md", "AGENTS.md", "PLAN.md"):
        assert self_doc in task.description


# -- Gap B: onboarding registers a first-class project record ----------------


def test_onboard_repository_creates_project_record(cp):
    cp.onboard_repository("https://github.com/o/widget.git")
    record = cp.get_project_record("widget")  # would raise NotFoundError pre-fix
    assert record.metadata.get("repository_url") == "https://github.com/o/widget.git"
    # get_project's summary must expose the repo association first-class and the
    # record must be non-null (it was None when onboarding made only a task).
    summary = cp.get_project("widget")
    assert summary["record"] is not None
    assert summary["summary"]["repository_url"] == "https://github.com/o/widget.git"


def test_onboard_repository_record_is_active_so_task_dispatches(cp):
    # The single onboarding task must be allowed to run; a paused project would
    # hold it forever. Active is the whole point of onboarding.
    cp.onboard_repository("https://github.com/o/widget.git")
    assert cp._project_dispatch_paused("widget") is False


def test_onboard_repository_is_idempotent(cp):
    cp.onboard_repository("https://github.com/o/widget.git")
    cp.onboard_repository("https://github.com/o/widget.git")
    records = [r for r in cp.list_project_records() if r.name == "widget"]
    assert len(records) == 1  # no duplicate project
    assert records[0].metadata.get("repository_url") == "https://github.com/o/widget.git"


def test_onboard_repository_records_default_branch(cp):
    cp.onboard_repository("https://github.com/o/widget.git", default_branch="develop")
    assert cp.get_project_record("widget").metadata.get("default_branch") == "develop"


def test_onboard_repository_preserves_existing_paused_state(cp):
    # A pre-existing project (operator may have paused it) must keep its
    # dispatch state; onboarding only fills in the missing repo URL.
    cp.create_project("widget", dispatch_paused=True)
    cp.onboard_repository("https://github.com/o/widget.git", project="widget")
    assert cp._project_dispatch_paused("widget") is True
    assert (
        cp.get_project_record("widget").metadata.get("repository_url")
        == "https://github.com/o/widget.git"
    )


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
    # Error must steer the caller to the onboarding path, not just fail.
    assert "onboard" in str(exc.value)
    # And no junk project named after the URL should exist.
    assert not [r for r in cp.list_project_records() if r.name == url]


def test_create_project_allows_normal_name(cp):
    assert cp.create_project("ova").name == "ova"
