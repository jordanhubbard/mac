"""Tests for GitHub-issue → mac-task ingestion (mac.github_ingest)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest

from mac.github_ingest import (
    GitHubIngestConfig,
    GitHubIssueIngestor,
    ProjectIngestPolicy,
    issue_origin_key,
    parse_github_owner_repo,
)


# --------------------------------------------------------------------------- #
# URL parsing / key helpers
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://github.com/jordanhubbard/mac", ("jordanhubbard", "mac")),
        ("https://github.com/jordanhubbard/mac.git", ("jordanhubbard", "mac")),
        ("git@github.com:NVIDIA-dev/ova.git", ("NVIDIA-dev", "ova")),
        ("ssh://git@github.com/owner/repo.git", ("owner", "repo")),
        ("github.com/owner/repo", ("owner", "repo")),
        ("https://gitea.example.com/owner/repo.git", None),
        ("https://github.com/owner", None),
        ("", None),
        ("not a url", None),
    ],
)
def test_parse_github_owner_repo(url, expected):
    assert parse_github_owner_repo(url) == expected


def test_issue_origin_key_normalizes_case():
    assert issue_origin_key("Owner", "Repo", 7) == "github:owner/repo#7"
    assert issue_origin_key("a", "b", "12") == "github:a/b#12"


# --------------------------------------------------------------------------- #
# Config / policy parsing
# --------------------------------------------------------------------------- #


def test_config_from_env_defaults_disabled():
    cfg = GitHubIngestConfig.from_env({})
    assert cfg.enabled is False
    assert cfg.active is False
    assert cfg.interval_seconds == pytest.approx(300.0)


def test_config_from_env_enabled_and_bounds():
    cfg = GitHubIngestConfig.from_env(
        {
            "MAC_GITHUB_INGEST_ENABLED": "true",
            "MAC_GITHUB_INGEST_INTERVAL_SECONDS": "120",
            "MAC_GITHUB_INGEST_MAX_ISSUES_PER_REPO": "5",
        }
    )
    assert cfg.enabled is True and cfg.active is True
    assert cfg.interval_seconds == pytest.approx(120.0)
    assert cfg.max_issues_per_repo == 5


def test_config_from_env_out_of_range_flags_error():
    cfg = GitHubIngestConfig.from_env(
        {"MAC_GITHUB_INGEST_ENABLED": "1", "MAC_GITHUB_INGEST_INTERVAL_SECONDS": "1"}
    )
    # 1s is below the floor -> configuration_error set -> not active.
    assert cfg.configuration_error
    assert cfg.active is False


def test_policy_defaults_disabled_when_absent():
    assert ProjectIngestPolicy.from_metadata({}).enabled is False
    assert ProjectIngestPolicy.from_metadata(None).enabled is False
    assert ProjectIngestPolicy.from_metadata(
        {"github_issue_ingest": "nonsense"}
    ).enabled is False


def test_policy_parses_fields():
    policy = ProjectIngestPolicy.from_metadata(
        {
            "github_issue_ingest": {
                "enabled": True,
                "labels": ["bug", "  ", "agent-ready", 3],
                "state": "weird",
                "default_capabilities": ["python"],
                "priority": "5",
                "auto_cancel_closed": True,
            }
        }
    )
    assert policy.enabled is True
    assert policy.labels == ("bug", "agent-ready")
    assert policy.state == "open"  # invalid value normalized
    assert policy.default_capabilities == ("python",)
    assert policy.priority == 5
    assert policy.auto_cancel_closed is True


# --------------------------------------------------------------------------- #
# Fakes for the ingestor
# --------------------------------------------------------------------------- #


@dataclass
class FakeTask:
    id: str
    project: str
    title: str
    metadata: Dict[str, Any]
    state: str = "open"


@dataclass
class FakeProject:
    name: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class FakeControlPlane:
    def __init__(self, projects: List[FakeProject], tasks: Optional[List[FakeTask]] = None):
        self._projects = projects
        self._tasks: List[FakeTask] = list(tasks or [])
        self._counter = 0
        self.logs: List[Dict[str, Any]] = []
        self.transitions: List[Dict[str, Any]] = []

    def list_project_records(self):
        return list(self._projects)

    def list_tasks(self):
        return list(self._tasks)

    def create_task(self, title, *, description="", project=None, priority=0,
                    required_capabilities=None, metadata=None, actor="human", **_):
        self._counter += 1
        task = FakeTask(
            id="task_%d" % self._counter,
            project=project or "",
            title=title,
            metadata=metadata or {},
        )
        self._tasks.append(task)
        return task

    def transition_task(self, task_id, target_state, actor, detail=None, **_):
        for task in self._tasks:
            if task.id == task_id:
                task.state = target_state
        self.transitions.append(
            {"task_id": task_id, "state": target_state, "detail": detail}
        )
        return None

    def record_log(self, *args, **kwargs):
        self.logs.append({"args": args, "kwargs": kwargs})
        return None


def _issue(number, title="t", *, body="b", labels=None, is_pr=False, updated="2026-01-01"):
    obj: Dict[str, Any] = {
        "number": number,
        "title": title,
        "body": body,
        "html_url": "https://github.com/o/r/issues/%d" % number,
        "labels": [{"name": name} for name in (labels or [])],
        "updated_at": updated,
    }
    if is_pr:
        obj["pull_request"] = {"url": "https://github.com/o/r/pull/%d" % number}
    return obj


def _opted_in_project(name="mac", url="https://github.com/o/r", **policy):
    md = {"repository_url": url, "github_issue_ingest": {"enabled": True, **policy}}
    return FakeProject(name=name, metadata=md)


def _ingestor(cp, issues_by_repo, *, token="tok", config=None):
    def fetcher(owner, repo, *, token, labels, state, limit):
        return list(issues_by_repo.get((owner, repo), []))[:limit]

    return GitHubIssueIngestor(
        cp,
        config or GitHubIngestConfig(enabled=True),
        issue_fetcher=fetcher,
        token_provider=lambda: token,
    )


# --------------------------------------------------------------------------- #
# Ingestor behavior
# --------------------------------------------------------------------------- #


def test_creates_tasks_for_open_issues():
    cp = FakeControlPlane([_opted_in_project()])
    ing = _ingestor(cp, {("o", "r"): [_issue(1), _issue(2)]})
    report = ing.run_once()
    assert report["created_count"] == 2
    keys = {t.metadata["origin"]["key"] for t in cp._tasks}
    assert keys == {"github:o/r#1", "github:o/r#2"}
    # tasks are filed under the project, so create_task couples them to the repo
    assert all(t.project == "mac" for t in cp._tasks)


def test_idempotent_on_repoll():
    cp = FakeControlPlane([_opted_in_project()])
    issues = {("o", "r"): [_issue(1), _issue(2)]}
    ing = _ingestor(cp, issues)
    ing.run_once()
    second = ing.run_once()
    # Second poll creates nothing; both issues are skipped as already-present.
    assert second["created_count"] == 0
    assert len(cp._tasks) == 2
    assert second["repositories"][0]["skipped"] == 2


def test_filters_out_pull_requests():
    cp = FakeControlPlane([_opted_in_project()])
    ing = _ingestor(cp, {("o", "r"): [_issue(1), _issue(2, is_pr=True)]})
    report = ing.run_once()
    assert report["created_count"] == 1
    assert cp._tasks[0].metadata["origin"]["number"] == 1


def test_skips_project_not_opted_in():
    proj = FakeProject(name="mac", metadata={"repository_url": "https://github.com/o/r"})
    cp = FakeControlPlane([proj])
    ing = _ingestor(cp, {("o", "r"): [_issue(1)]})
    report = ing.run_once()
    assert report["created_count"] == 0
    assert cp._tasks == []


def test_skips_non_github_repo():
    proj = _opted_in_project(url="https://gitea.example.com/o/r")
    cp = FakeControlPlane([proj])
    ing = _ingestor(cp, {("o", "r"): [_issue(1)]})
    report = ing.run_once()
    assert report["created_count"] == 0
    assert "no github repository_url" in report["repositories"][0].get("skipped_reason", "")


def test_missing_token_records_error_no_tasks():
    cp = FakeControlPlane([_opted_in_project()])
    ing = _ingestor(cp, {("o", "r"): [_issue(1)]}, token="")
    report = ing.run_once()
    assert report["created_count"] == 0
    assert "token" in report["repositories"][0]["error"]


def test_backpressure_caps_open_tasks():
    cp = FakeControlPlane([_opted_in_project()])
    config = GitHubIngestConfig(enabled=True, max_open_tasks_per_project=1)
    ing = _ingestor(cp, {("o", "r"): [_issue(1), _issue(2), _issue(3)]}, config=config)
    report = ing.run_once()
    assert report["created_count"] == 1
    assert report["repositories"][0]["backpressure"] == 1


def test_per_repo_fetch_error_isolated():
    cp = FakeControlPlane([_opted_in_project(name="a", url="https://github.com/o/a"),
                           _opted_in_project(name="b", url="https://github.com/o/b")])

    def fetcher(owner, repo, *, token, labels, state, limit):
        if repo == "a":
            raise RuntimeError("boom")
        return [_issue(1)]

    ing = GitHubIssueIngestor(
        cp, GitHubIngestConfig(enabled=True), issue_fetcher=fetcher,
        token_provider=lambda: "tok",
    )
    report = ing.run_once()
    # repo a errored, repo b still produced a task
    assert report["created_count"] == 1
    errored = [r for r in report["repositories"] if r.get("error")]
    assert errored and "boom" in errored[0]["error"]


def test_auto_cancel_closed_cancels_open_tasks_only():
    # Two existing github-issue tasks; only #1 is still open on GitHub.
    open_task = FakeTask(
        id="task_open", project="mac", title="one", state="open",
        metadata={"origin": {"type": "github_issue", "key": "github:o/r#1"}},
    )
    running_task = FakeTask(
        id="task_running", project="mac", title="two", state="running",
        metadata={"origin": {"type": "github_issue", "key": "github:o/r#2"}},
    )
    stale_open = FakeTask(
        id="task_stale", project="mac", title="three", state="open",
        metadata={"origin": {"type": "github_issue", "key": "github:o/r#3"}},
    )
    cp = FakeControlPlane([_opted_in_project(auto_cancel_closed=True)],
                          tasks=[open_task, running_task, stale_open])
    ing = _ingestor(cp, {("o", "r"): [_issue(1)]})  # only #1 open now
    report = ing.run_once()
    # #3 open task -> cancelled; #2 running -> left alone; #1 still open -> kept
    assert report["cancelled_count"] == 1
    assert stale_open.state == "cancelled"
    assert running_task.state == "running"
    assert open_task.state == "open"


def test_disabled_ingestor_does_not_start():
    cp = FakeControlPlane([_opted_in_project()])
    ing = GitHubIssueIngestor(cp, GitHubIngestConfig(enabled=False))
    assert ing.start() is False
    status = ing.status()
    assert status["thread_alive"] is False
