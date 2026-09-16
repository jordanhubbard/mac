"""Tests for autonomous per-repo backlog grooming (mac.backlog_groomer)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import pytest

from mac.backlog_groomer import (
    BacklogGroomer,
    BacklogGroomerConfig,
    ProjectGroomingPolicy,
    build_grooming_description,
)
from mac.executor_scope import maybe_auto_decompose
from mac.executor_prompt import task_evidence_type
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="microseconds")


# --------------------------------------------------------------------------- #
# Config / policy
# --------------------------------------------------------------------------- #


def test_config_defaults_disabled():
    cfg = BacklogGroomerConfig.from_env({})
    assert cfg.enabled is False and cfg.active is False


def test_config_enabled_and_bounds():
    cfg = BacklogGroomerConfig.from_env(
        {
            "MAC_BACKLOG_GROOM_ENABLED": "1",
            "MAC_BACKLOG_GROOM_MIN_READY": "3",
            "MAC_BACKLOG_GROOM_BACKLOG_SIZE": "7",
        }
    )
    assert cfg.active is True and cfg.min_ready == 3 and cfg.backlog_size == 7


def test_config_out_of_range_flags_error():
    cfg = BacklogGroomerConfig.from_env(
        {
            "MAC_BACKLOG_GROOM_ENABLED": "1",
            "MAC_BACKLOG_GROOM_INTERVAL_SECONDS": "1",  # below floor
        }
    )
    assert cfg.configuration_error and cfg.active is False


def test_policy_parsing():
    p = ProjectGroomingPolicy.from_metadata(
        {
            "backlog_grooming": {
                "enabled": True,
                "backlog_size": "8",
                "min_ready": 1,
                "default_capabilities": ["python", " ", 3],
            }
        }
    )
    assert p.enabled and p.backlog_size == 8 and p.min_ready == 1
    assert p.default_capabilities == ("python",)
    assert ProjectGroomingPolicy.from_metadata({}).enabled is False


def test_description_mentions_plan_steps_and_size():
    d = build_grooming_description("mac", "https://github.com/o/r", 5)
    assert "plan_steps" in d and "5 concrete" in d and "READ-ONLY" in d
    assert "evidence_type=operator_result" in d


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


@dataclass
class FakeTask:
    id: str
    project: str
    state: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: str = _iso(datetime(2020, 1, 1, tzinfo=timezone.utc))


@dataclass
class FakeProject:
    name: str
    metadata: Dict[str, Any] = field(default_factory=dict)


class FakeCP:
    def __init__(self, projects, tasks=None):
        self._projects = projects
        self._tasks: List[FakeTask] = list(tasks or [])
        self._n = 0
        self.created: List[Dict[str, Any]] = []
        self.logs: List[Any] = []

    def list_project_records(self):
        return list(self._projects)

    def list_tasks(self):
        return list(self._tasks)

    def ready_tasks(self):
        return [
            task
            for task in self._tasks
            if task.state == "open" and not bool(task.metadata.get("no_dispatch"))
        ]

    def create_task(
        self,
        title,
        *,
        description="",
        project=None,
        priority=0,
        required_capabilities=None,
        metadata=None,
        actor="human",
        **_,
    ):
        self._n += 1
        t = FakeTask(
            id="task_%d" % self._n, project=project or "", state="open", metadata=metadata or {}
        )
        self._tasks.append(t)
        self.created.append(
            {"title": title, "project": project, "metadata": metadata, "description": description}
        )
        return t

    def record_log(self, *a, **k):
        self.logs.append((a, k))


class FailingReadyCP(FakeCP):
    def ready_tasks(self):
        raise RuntimeError("ready query failed")


def _proj(name="mac", url="https://github.com/o/r", **groom):
    md = {"repository_url": url, "backlog_grooming": {"enabled": True, **groom}}
    return FakeProject(name=name, metadata=md)


def _groomer(cp, **cfg):
    base = {"enabled": True, "min_ready": 2, "regroom_interval_seconds": 3600, "backlog_size": 5}
    base.update(cfg)
    return BacklogGroomer(cp, BacklogGroomerConfig(**base))


# --------------------------------------------------------------------------- #
# Behavior
# --------------------------------------------------------------------------- #


def test_grooms_idle_opted_in_project():
    cp = FakeCP([_proj()])  # no tasks -> idle
    report = _groomer(cp).run_once()
    assert report["groomed_count"] == 1
    created = cp.created[0]
    assert created["project"] == "mac"
    assert created["metadata"]["origin"]["type"] == "backlog_grooming"
    # Repo context is requested, but no evidence override is supplied.
    assert created["metadata"]["origin"]["repository_url"] == "https://github.com/o/r"
    assert created["metadata"]["deliverable"] == "report"
    assert created["metadata"]["report_repository_access"] == {
        "schema": "mac.report_repository_access.v1",
        "mode": "read_only",
    }
    assert "evidence_type" not in created["metadata"]


def test_grooming_task_passes_real_control_plane_normalization(tmp_path, monkeypatch):
    cp = ControlPlane(ephemeral_store(), secret_key="backlog-groomer-test-secret-key-32+")
    repo = tmp_path / "mac"
    contract_dir = repo / ".mac"
    contract_dir.mkdir(parents=True)
    (contract_dir / "project.yaml").write_text(
        "\n".join(
            [
                "schema: mac.repository_contract.v1",
                "project: mac",
                "canonical_remote_url: https://github.com/o/r",
                "default_branch: main",
                "platforms: [darwin, linux, wsl2]",
                "toolchain:",
                "  required_commands: [python3]",
                "bootstrap:",
                "  command: python3 scripts/bootstrap-project.py",
                "  creates: [.venv/bin/python]",
                "test:",
                "  command: pytest",
                "evidence:",
                "  required: [tests]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cp.create_project(
        "mac",
        metadata={
            "repository_url": "https://github.com/o/r",
            "backlog_grooming": {"enabled": True},
        },
    )
    cp.register_project_repository("mac", str(repo), project="mac")

    report = _groomer(cp).run_once()

    assert report["groomed_count"] == 1
    task = cp.get_task(report["projects"][0]["task_id"])
    assert "evidence_type" not in task.metadata
    assert task.metadata["report_repository_access"] == {
        "schema": "mac.report_repository_access.v1",
        "mode": "read_only",
    }
    assert task.metadata["execution_contract"]["type"] == "repository"
    assert task.metadata["execution_contract"]["repository_contract"]["project"] == "mac"
    assert task_evidence_type(task.to_dict()) == "operator_result"
    assert "plan_steps" in task.description

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "mac-evidence.json").write_text(
        '{"evidence_type":"operator_result","summary":"Prioritized work.",'
        '"plan_steps":[{"title":"Add coverage","description":"Cover the gap."}]}',
        encoding="utf-8",
    )
    posted = {}
    monkeypatch.setattr(
        "mac.executor_scope._hub_post_child_tasks",
        lambda task_id, children: posted.update(task_id=task_id, children=children) or {},
    )
    assert maybe_auto_decompose(workspace, task.to_dict()) is True
    assert posted["task_id"] == task.id
    assert posted["children"] == [{"title": "Add coverage", "description": "Cover the gap."}]


def test_skips_project_not_opted_in():
    cp = FakeCP([FakeProject("mac", {"repository_url": "https://github.com/o/r"})])
    report = _groomer(cp).run_once()
    assert report["groomed_count"] == 0


def test_skips_when_not_idle():
    # "Idle" means the dispatch-ready backlog is already at its threshold.
    tasks = [FakeTask("a", "mac", "open"), FakeTask("b", "mac", "open")]
    cp = FakeCP([_proj()], tasks=tasks)
    report = _groomer(cp).run_once()
    assert report["groomed_count"] == 0
    result = report["projects"][0]
    assert result["active_tasks"] == 2
    assert result["ready_tasks"] == 2
    assert "ready backlog sufficient" in result["skipped_reason"]


def test_parked_and_in_flight_work_does_not_suppress_grooming():
    tasks = [
        FakeTask("held", "mac", "open", {"no_dispatch": True}),
        FakeTask("blocked", "mac", "blocked"),
        FakeTask("running", "mac", "running"),
        FakeTask("reviewing", "mac", "reviewing"),
    ]
    cp = FakeCP([_proj()], tasks=tasks)
    report = _groomer(cp).run_once()
    assert report["groomed_count"] == 1
    result = report["projects"][0]
    assert result["active_tasks"] == 4
    assert result["ready_tasks"] == 0


def test_grooming_tasks_do_not_count_as_project_work():
    # An open grooming task must NOT satisfy the idle threshold (else grooming
    # would suppress itself), but it DOES block stacking another.
    groom = FakeTask(
        "g",
        "mac",
        "open",
        {"origin": {"type": "backlog_grooming"}},
        created_at=_iso(datetime.now(timezone.utc)),
    )
    cp = FakeCP([_proj()], tasks=[groom])
    report = _groomer(cp).run_once()
    assert report["groomed_count"] == 0
    assert report["projects"][0]["skipped_reason"] == "grooming task already open"


def test_completed_grooming_task_does_not_count_as_ready_work():
    old = FakeTask(
        "g",
        "mac",
        "open",
        {"origin": {"type": "backlog_grooming"}},
        created_at=_iso(datetime.now(timezone.utc) - timedelta(hours=8)),
    )
    cp = FakeCP([_proj()], tasks=[old])
    report = _groomer(cp, regroom_interval_seconds=3600).run_once()
    assert report["groomed_count"] == 0
    assert report["projects"][0]["ready_tasks"] == 0
    assert report["projects"][0]["skipped_reason"] == "grooming task already open"


def test_ready_snapshot_failure_skips_grooming():
    cp = FailingReadyCP([_proj()])
    report = _groomer(cp).run_once()
    result = report["projects"][0]
    assert report["groomed_count"] == 0
    assert result["error"] == "ready task snapshot unavailable"
    assert result["skipped_reason"] == "could not determine ready backlog"


def test_skips_non_repo_project():
    cp = FakeCP([FakeProject("mac", {"backlog_grooming": {"enabled": True}})])
    report = _groomer(cp).run_once()
    assert report["projects"][0]["skipped_reason"] == "no repository_url"


def test_cadence_blocks_regroom():
    # A completed grooming task 10 minutes ago; regroom interval is 1h -> skip.
    recent = FakeTask(
        "g",
        "mac",
        "completed",
        {"origin": {"type": "backlog_grooming"}},
        created_at=_iso(datetime.now(timezone.utc) - timedelta(minutes=10)),
    )
    cp = FakeCP([_proj()], tasks=[recent])
    report = _groomer(cp, regroom_interval_seconds=3600).run_once()
    assert report["groomed_count"] == 0
    assert "ago" in report["projects"][0]["skipped_reason"]


def test_regrooms_after_cadence_elapses():
    old = FakeTask(
        "g",
        "mac",
        "completed",
        {"origin": {"type": "backlog_grooming"}},
        created_at=_iso(datetime.now(timezone.utc) - timedelta(hours=8)),
    )
    cp = FakeCP([_proj()], tasks=[old])
    report = _groomer(cp, regroom_interval_seconds=3600).run_once()
    assert report["groomed_count"] == 1


def test_disabled_groomer_does_not_start():
    cp = FakeCP([_proj()])
    g = BacklogGroomer(cp, BacklogGroomerConfig(enabled=False))
    assert g.start() is False
    assert g.status()["thread_alive"] is False
