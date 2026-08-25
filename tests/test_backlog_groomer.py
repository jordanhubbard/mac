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
    # repo-coupled (origin has url) but investigation-gated, not code
    assert created["metadata"]["origin"]["repository_url"] == "https://github.com/o/r"
    assert created["metadata"]["evidence_type"] == "investigation"


def test_skips_project_not_opted_in():
    cp = FakeCP([FakeProject("mac", {"repository_url": "https://github.com/o/r"})])
    report = _groomer(cp).run_once()
    assert report["groomed_count"] == 0


def test_skips_when_not_idle():
    # 2 pending tasks meets min_ready=2 -> not idle
    tasks = [FakeTask("a", "mac", "open"), FakeTask("b", "mac", "running")]
    cp = FakeCP([_proj()], tasks=tasks)
    report = _groomer(cp).run_once()
    assert report["groomed_count"] == 0
    assert "not idle" in report["projects"][0]["skipped_reason"]


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
