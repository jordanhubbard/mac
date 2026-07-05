from __future__ import annotations

import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from mac import cli
from mac.cli import main


def _git(repo, *args):
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return result.stdout.strip()


def _run(tmp_path, *args):
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--json", "--db", str(tmp_path / "mac.db"), *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None, err.getvalue()


def _repository(tmp_path):
    remote = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    remote.mkdir()
    repo.mkdir()
    _git(remote, "init", "--bare")
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "mac-tests@example.invalid")
    _git(repo, "config", "user.name", "mac tests")
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "push", "-u", "origin", "main")
    return repo, remote


def test_task_close_cli_records_structured_cancellation(tmp_path):
    rc, task, _error = _run(tmp_path, "task", "create", "old work")
    assert rc == 0
    replacement = "task_" + "b" * 32

    rc, cancelled, error = _run(
        tmp_path,
        "task",
        "close",
        task["id"],
        "--cancelled",
        "--reason",
        "replacement merged",
        "--disposition",
        "superseded",
        "--replacement-task",
        replacement,
        "--cleanup-grace-days",
        "0",
    )

    assert rc == 0, error
    lifecycle = cancelled["metadata"]["repository_ref_lifecycle"]
    assert lifecycle["disposition"] == "superseded"
    assert lifecycle["replacement_task_id"] == replacement
    assert lifecycle["eligible_after"] == lifecycle["terminal_at"]


def test_task_close_cli_requires_cancellation_reason(tmp_path):
    rc, task, _error = _run(tmp_path, "task", "create", "reason required")
    assert rc == 0

    rc, result, error = _run(
        tmp_path,
        "task",
        "close",
        task["id"],
        "--cancelled",
    )

    assert rc == 1
    assert result is None
    assert "--reason is required with --cancelled" in error


def test_task_close_cli_refuses_unlinked_duplicate(tmp_path):
    rc, task, _error = _run(tmp_path, "task", "create", "duplicate work")
    assert rc == 0

    rc, result, error = _run(
        tmp_path,
        "task",
        "close",
        task["id"],
        "--cancelled",
        "--reason",
        "duplicate",
        "--disposition",
        "duplicate",
    )

    assert rc == 1
    assert result is None
    assert "replacement_task_id" in error


def test_repo_refs_audit_dry_run_and_execute(tmp_path, monkeypatch):
    repo, _remote = _repository(tmp_path)
    rc, task, _error = _run(tmp_path, "task", "create", "obsolete work")
    assert rc == 0
    rc, replacement_task, _error = _run(
        tmp_path, "task", "create", "replacement work"
    )
    assert rc == 0
    rc, _completed, error = _run(
        tmp_path,
        "task",
        "force-complete",
        replacement_task["id"],
        "--reason",
        "replacement merged",
    )
    assert rc == 0, error
    replacement = replacement_task["id"]
    rc, _cancelled, error = _run(
        tmp_path,
        "task",
        "close",
        task["id"],
        "--cancelled",
        "--reason",
        "replacement merged",
        "--disposition",
        "superseded",
        "--replacement-task",
        replacement,
        "--cleanup-grace-days",
        "0",
    )
    assert rc == 0, error

    branch = "mac/agent_worker/%s-lease_%s" % (task["id"], "d" * 18)
    _git(repo, "push", "origin", "HEAD:refs/heads/%s" % branch)
    monkeypatch.setattr(cli, "_repository_open_pull_requests", lambda _repo: ({}, ""))

    rc, audit, error = _run(
        tmp_path,
        "repo",
        "refs",
        "audit",
        "--repo",
        str(repo),
        "--grace-days",
        "0",
    )
    assert rc == 0, error
    assert audit["eligible_count"] == 1
    assert audit["refs"][0]["classification"] == "superseded"

    rc, dry_run, error = _run(
        tmp_path,
        "repo",
        "refs",
        "prune",
        "--repo",
        str(repo),
        "--dry-run",
        "--grace-days",
        "0",
    )
    assert rc == 0, error
    assert dry_run["mode"] == "dry-run"
    assert _git(repo, "ls-remote", "--heads", "origin", "refs/heads/%s" % branch)

    rc, executed, error = _run(
        tmp_path,
        "repo",
        "refs",
        "prune",
        "--repo",
        str(repo),
        "--execute",
        "--grace-days",
        "0",
        "--actor",
        "operator",
    )
    assert rc == 0, error
    assert executed["mode"] == "execute"
    assert executed["count"] == 1
    assert not _git(
        repo, "ls-remote", "--heads", "origin", "refs/heads/%s" % branch
    )

    rc, detail, error = _run(tmp_path, "task", "show", task["id"])
    assert rc == 0, error
    cleanup = [
        item
        for item in detail["evidence"]
        if item["metadata"].get("schema") == "mac.repository_ref_cleanup.v1"
    ]
    assert [item["metadata"]["action"] for item in cleanup] == [
        "requested",
        "deleted",
    ]


def test_repo_refs_execute_requires_pull_request_verification(tmp_path, monkeypatch):
    repo, _remote = _repository(tmp_path)
    monkeypatch.setattr(
        cli,
        "_repository_open_pull_requests",
        lambda _repo: (None, "pull request state unavailable"),
    )

    rc, result, error = _run(
        tmp_path,
        "repo",
        "refs",
        "prune",
        "--repo",
        str(repo),
        "--execute",
    )

    assert rc == 1
    assert result is None
    assert "refusing executable cleanup" in error


def test_open_pull_request_probe_parses_and_validates_output(tmp_path, monkeypatch):
    payload = [
        {"headRefName": "branch-a", "number": 12, "url": "https://example/pr/12"},
        {"headRefName": "branch-b", "number": 13, "url": ""},
        {"headRefName": "branch-c", "number": "", "url": ""},
        {"headRefName": "", "number": 14, "url": "https://example/pr/14"},
        "invalid",
    ]
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 0, json.dumps(payload), ""
        ),
    )

    heads, warning = cli._repository_open_pull_requests(tmp_path)

    assert warning == ""
    assert heads == {
        "branch-a": "https://example/pr/12",
        "branch-b": "PR #13",
        "branch-c": "open pull request",
    }


@pytest.mark.parametrize(
    ("behavior", "warning"),
    [
        ("raise", "could not be verified"),
        ("failure", "could not be verified"),
        ("malformed", "malformed JSON"),
        ("wrong-shape", "invalid response"),
    ],
)
def test_open_pull_request_probe_fails_closed(tmp_path, monkeypatch, behavior, warning):
    def fake_run(*args, **kwargs):
        if behavior == "raise":
            raise FileNotFoundError
        if behavior == "failure":
            return subprocess.CompletedProcess(args[0], 1, "", "failed")
        if behavior == "malformed":
            return subprocess.CompletedProcess(args[0], 0, "{", "")
        return subprocess.CompletedProcess(args[0], 0, "{}", "")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    heads, message = cli._repository_open_pull_requests(tmp_path)
    assert heads is None
    assert warning in message


def test_repository_ref_audit_filters_tasks_and_reports_warning(tmp_path, monkeypatch):
    task_id = "task_" + "a" * 32
    other_id = "task_" + "b" * 32
    refs = [SimpleNamespace(task_id=task_id), SimpleNamespace(task_id=other_id)]
    plane = SimpleNamespace(task_detail=lambda _task_id: {})
    captured = {}
    monkeypatch.setattr(cli, "_plane", lambda _args: plane)
    monkeypatch.setattr(cli, "list_managed_remote_refs", lambda _repo, _remote: refs)
    monkeypatch.setattr(
        cli,
        "_repository_open_pull_requests",
        lambda _repo: (None, "PR state unavailable"),
    )
    monkeypatch.setattr(
        cli,
        "audit_repository_refs",
        lambda _repo, selected, _loader, **kwargs: captured.update(
            selected=list(selected), kwargs=kwargs
        )
        or [],
    )
    args = SimpleNamespace(
        repo_path=str(tmp_path),
        remote="origin",
        task_ids=[task_id],
        base_ref=None,
        grace_days=2,
    )

    returned_plane, audits, warning = cli._repository_ref_audit(args)

    assert returned_plane is plane
    assert audits == []
    assert warning == "PR state unavailable"
    assert captured["selected"] == [refs[0]]
    assert captured["kwargs"]["base_ref"] == "origin/main"
    assert captured["kwargs"]["default_grace_seconds"] == 2 * 24 * 60 * 60
    report = cli._repository_ref_report([], pr_warning=warning)
    assert report["warning"] == warning


def test_repository_ref_audit_rejects_missing_repo(tmp_path):
    args = SimpleNamespace(repo_path=str(tmp_path / "missing"))
    with pytest.raises(cli.RepositoryHygieneError, match="does not exist"):
        cli._repository_ref_audit(args)


def test_repo_refs_reconciler_status_and_trigger_commands(monkeypatch, capsys):
    class Plane:
        def repository_ref_reconciler_status(self):
            return {"status": "idle"}

        def reconcile_repository_refs(self, *, mode, actor):
            return {"status": "completed", "mode": mode, "actor": actor}

    monkeypatch.setattr(cli, "_plane", lambda _args: Plane())

    assert main(["--json", "repo", "refs", "status"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "idle"}

    assert main(
        [
            "--json",
            "repo",
            "refs",
            "reconcile",
            "--mode",
            "audit",
            "--actor",
            "operator",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out) == {
        "status": "completed",
        "mode": "audit",
        "actor": "operator",
    }
