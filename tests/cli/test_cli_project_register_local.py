from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from mac import cli
from mac.models import MACError


class _Plane:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def register_project(self, repository_url: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((repository_url, kwargs))
        return {"repository_url": repository_url, **kwargs}


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _checkout(
    tmp_path: Path,
    *,
    branch: str = "topic/current",
    origin: str = "git@github.com:example/widget.git",
    origin_head: str | None = "main",
) -> Path:
    repo = tmp_path / "widget"
    subprocess.run(
        ["git", "init", "-q", "-b", branch, str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git(repo, "remote", "add", "origin", origin)
    if origin_head:
        _git(
            repo,
            "symbolic-ref",
            "refs/remotes/origin/HEAD",
            "refs/remotes/origin/%s" % origin_head,
        )
    return repo


def test_project_register_without_target_discovers_cwd(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = _checkout(tmp_path)
    plane = _Plane()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)

    assert cli.main(["project", "register"]) == 0

    assert plane.calls == [
        (
            "git@github.com:example/widget.git",
            {
                "project": None,
                "default_branch": "main",
                "title": None,
                "priority": 0,
                "required_capabilities": None,
                "actor": "human",
            },
        )
    ]
    assert "registering checkout %s (branch main)" % repo.resolve() in (
        capsys.readouterr().err
    )


def test_project_register_dot_falls_back_to_current_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _checkout(tmp_path, origin_head=None)
    plane = _Plane()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)

    assert cli.main(["project", "register", "."]) == 0

    assert plane.calls[0][0] == "git@github.com:example/widget.git"
    assert plane.calls[0][1]["default_branch"] == "topic/current"


def test_project_register_accepts_absolute_path_and_branch_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _checkout(tmp_path)
    plane = _Plane()
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)

    assert (
        cli.main(
            [
                "project",
                "register",
                str(repo),
                "--branch",
                "release/next",
                "--project",
                "custom",
            ]
        )
        == 0
    )

    assert plane.calls[0][0] == "git@github.com:example/widget.git"
    assert plane.calls[0][1]["default_branch"] == "release/next"
    assert plane.calls[0][1]["project"] == "custom"


def test_project_register_url_behavior_is_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plane = _Plane()
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)

    registration = "git@github.com:example/widget.git#feature/one"
    assert cli.main(["project", "register", registration]) == 0

    assert plane.calls[0][0] == registration
    assert plane.calls[0][1]["default_branch"] is None


def test_project_register_local_checkout_requires_origin(tmp_path: Path) -> None:
    repo = tmp_path / "widget"
    subprocess.run(
        ["git", "init", "-q", "-b", "main", str(repo)],
        check=True,
        capture_output=True,
        text=True,
    )

    with pytest.raises(MACError, match="remote get-url origin"):
        cli._resolve_project_registration_target(
            str(repo),
            default_branch=None,
        )


def test_project_register_local_checkout_rejects_credentialed_origin(
    tmp_path: Path,
) -> None:
    repo = _checkout(
        tmp_path,
        origin="https://user:secret@example.com/org/widget.git",
    )

    with pytest.raises(MACError, match="secret-free"):
        cli._resolve_project_registration_target(
            str(repo),
            default_branch=None,
        )
