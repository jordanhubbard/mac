from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from mac import cicd_monitor
from mac.cicd_monitor import (
    CICDMonitor,
    CICDMonitorConfig,
    ProjectCICDPolicy,
    cadence_seconds_for_latency,
    post_publication_delay_hours,
)


SHA_A = "a" * 40
SHA_B = "b" * 40
REPOSITORY_URL = "https://github.com/acme/widgets.git"


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, **kwargs: float) -> None:
        self.value += timedelta(**kwargs)


@dataclass
class FakeProject:
    name: str
    metadata: dict


@dataclass
class FakeEvent:
    name: str
    subject_type: str
    subject_id: str
    detail: dict
    created_at: str


class FakeControlPlane:
    def __init__(self, clock: MutableClock, projects: list[FakeProject]) -> None:
        self.clock = clock
        self.projects = projects
        self.events: list[FakeEvent] = []
        self.tasks: list[SimpleNamespace] = []
        self._idempotent_tasks: dict[tuple[str, str], SimpleNamespace] = {}

    def list_project_records(self) -> list[FakeProject]:
        return list(self.projects)

    def record_log(
        self,
        name: str,
        *,
        layer: str,
        source: str,
        level: str,
        subject_type: str,
        subject_id: str,
        detail: dict,
    ) -> None:
        del layer, source, level
        self.events.insert(
            0,
            FakeEvent(
                name=name,
                subject_type=subject_type,
                subject_id=subject_id,
                detail=dict(detail),
                created_at=self.clock().isoformat(),
            ),
        )

    def list_observability(
        self,
        *,
        kind: str,
        name: str,
        limit: int,
        subject_type: str | None = None,
        subject_id: str | None = None,
    ) -> list[FakeEvent]:
        assert kind == "log"
        matches = [
            event
            for event in self.events
            if event.name == name
            and (subject_type is None or event.subject_type == subject_type)
            and (subject_id is None or event.subject_id == subject_id)
        ]
        return matches[:limit]

    def create_task(self, title: str, **kwargs: object) -> SimpleNamespace:
        key = (
            str(kwargs.get("_idempotency_scope") or ""),
            str(kwargs.get("idempotency_key") or ""),
        )
        existing = self._idempotent_tasks.get(key)
        if existing is not None:
            return existing
        task = SimpleNamespace(
            id=f"task-{len(self.tasks) + 1}",
            title=title,
            project=kwargs.get("project"),
            metadata=kwargs.get("metadata"),
            state="open",
            kwargs=kwargs,
        )
        self.tasks.append(task)
        self._idempotent_tasks[key] = task
        return task

    def get_task(self, task_id: str) -> SimpleNamespace:
        return next(task for task in self.tasks if task.id == task_id)


class FakeReconciliation:
    def __init__(self, claim: object | None = None) -> None:
        self.next_claim = (
            SimpleNamespace(name="cicd-monitor", cursor="cursor") if claim is None else claim
        )
        self.completed: list[tuple[object, str | None]] = []
        self.abandoned: list[object] = []

    def claim(self, name: str) -> object | None:
        assert name == "cicd-monitor"
        return self.next_claim

    def complete(self, claim: object, *, cursor: str | None) -> bool:
        self.completed.append((claim, cursor))
        return True

    def abandon(self, claim: object) -> bool:
        self.abandoned.append(claim)
        return True


def project(
    name: str = "widgets",
    *,
    repository_url: str = REPOSITORY_URL,
    cicd_monitor_policy: object | None = None,
) -> FakeProject:
    metadata: dict[str, object] = {"repository_url": repository_url}
    if cicd_monitor_policy is not None:
        metadata["cicd_monitor"] = cicd_monitor_policy
    return FakeProject(name=name, metadata=metadata)


def run(
    *,
    sha: str = SHA_A,
    conclusion: str = "success",
    status: str = "completed",
    duration_hours: float = 1,
    workflow_id: int = 10,
    name: str = "tests",
    clock: MutableClock,
) -> dict:
    started = clock() - timedelta(hours=duration_hours)
    return {
        "id": workflow_id * 100,
        "workflow_id": workflow_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": sha,
        "head_branch": "main",
        "event": "push",
        "html_url": f"https://github.com/acme/widgets/actions/runs/{workflow_id * 100}",
        "run_number": 7,
        "run_attempt": 1,
        "created_at": started.isoformat(),
        "run_started_at": started.isoformat(),
        "updated_at": clock().isoformat(),
    }


def payload(*runs: dict, workflows: bool = True) -> dict:
    return {
        "workflows": {"workflows": [{"id": 10, "name": "tests"}] if workflows else []},
        "runs": {"workflow_runs": list(runs)},
    }


class SequenceFetcher:
    def __init__(self, *responses: dict) -> None:
        self.responses = list(responses)
        self.calls: list[dict] = []

    def __call__(self, owner: str, repo: str, **kwargs: object) -> dict:
        self.calls.append({"owner": owner, "repo": repo, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected fetch")
        return self.responses.pop(0)


def monitor(
    cp: FakeControlPlane,
    clock: MutableClock,
    fetcher: SequenceFetcher,
    **config: object,
) -> CICDMonitor:
    return CICDMonitor(
        cp,
        CICDMonitorConfig(
            initial_delay_seconds=0,
            pending_retry_seconds=30 * 60,
            absent_recheck_seconds=24 * 60 * 60,
            **config,
        ),
        actions_fetcher=fetcher,
        token_provider=lambda: "token",
        now=clock,
        reconciliation=FakeReconciliation(),
    )


def test_config_is_default_on_and_supports_explicit_opt_out() -> None:
    assert CICDMonitorConfig.from_env({}).active is True
    assert CICDMonitorConfig.from_env({"MAC_CICD_MONITOR_ENABLED": "false"}).active is False

    invalid = CICDMonitorConfig.from_env({"MAC_CICD_MONITOR_ENABLED": "sometimes"})
    assert invalid.active is False
    assert "must be a boolean" in invalid.configuration_error


def test_project_policy_defaults_on_only_for_registered_github_repositories() -> None:
    assert ProjectCICDPolicy.from_metadata({}, REPOSITORY_URL).enabled is True
    assert ProjectCICDPolicy.from_metadata({"cicd_monitor": False}, REPOSITORY_URL).enabled is False
    assert (
        ProjectCICDPolicy.from_metadata(
            {"cicd_monitor": {"enabled": "false"}}, REPOSITORY_URL
        ).enabled
        is False
    )
    assert ProjectCICDPolicy.from_metadata({}, "https://gitlab.com/acme/widgets").enabled is False

    configured = ProjectCICDPolicy.from_metadata(
        {
            "cicd_monitor": {
                "default_branch": "trunk",
                "post_publication_delay_hours": 99,
                "required_capabilities": ["github", "ci"],
                "priority": 7,
            }
        },
        REPOSITORY_URL,
    )
    assert configured.default_branch == "trunk"
    assert configured.post_publication_delay_hours == 8
    assert configured.required_capabilities == ("github", "ci")
    assert configured.priority == 7


@pytest.mark.parametrize(
    ("average", "expected"),
    [
        (None, 6 * 60 * 60),
        (2 * 60 * 60, 4 * 60 * 60),
        (2 * 60 * 60 + 1, 8 * 60 * 60),
    ],
)
def test_cadence_uses_observed_average_latency(average: float | None, expected: float) -> None:
    assert cadence_seconds_for_latency(average) == expected


def test_publication_delay_is_clamped_to_one_through_eight_hours() -> None:
    assert post_publication_delay_hours(60) == 1
    assert post_publication_delay_hours(3 * 60 * 60) == 3
    assert post_publication_delay_hours(20 * 60 * 60) == 8
    assert post_publication_delay_hours(None, configured_hours=0.25) == 1


def test_http_fetcher_filters_by_exact_sha_or_default_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls: list[str] = []

    def fake_http_json(url: str, *, token: str, timeout: float) -> dict:
        assert token == "secret"
        assert timeout == 12
        urls.append(url)
        return {}

    monkeypatch.setattr(cicd_monitor, "_http_json", fake_http_json)
    cicd_monitor._http_actions_status(
        "acme",
        "widgets",
        token="secret",
        branch="main",
        head_sha=SHA_A,
        timeout=12,
    )
    exact_query = parse_qs(urlparse(urls[-1]).query)
    assert exact_query["head_sha"] == [SHA_A]
    assert "branch" not in exact_query

    cicd_monitor._http_actions_status(
        "acme",
        "widgets",
        token="secret",
        branch="trunk",
        timeout=12,
    )
    branch_query = parse_qs(urlparse(urls[-1]).query)
    assert branch_query["branch"] == ["trunk"]
    assert "head_sha" not in branch_query


def test_post_publication_schedule_is_durable_idempotent_and_exact_sha() -> None:
    clock = MutableClock()
    cp = FakeControlPlane(clock, [project()])
    fetcher = SequenceFetcher(
        payload(run(clock=clock)),
        payload(run(clock=clock)),
    )
    service = monitor(cp, clock, fetcher)

    scheduled = service.schedule_publication_followup(
        task_id="source-task",
        publication_id="publication-1",
        project="widgets",
        canonical_sha=SHA_A,
        published_at=clock().isoformat(),
    )
    duplicate = service.schedule_publication_followup(
        task_id="source-task",
        publication_id="publication-1",
        project="widgets",
        canonical_sha=SHA_A,
        published_at=clock().isoformat(),
    )
    assert scheduled["status"] == "scheduled"
    assert scheduled["delay_hours"] == 2
    assert duplicate["status"] == "already_scheduled"

    assert service.run_once()["checked_count"] == 1
    assert fetcher.calls[0]["head_sha"] == ""

    clock.advance(hours=2)
    report = service.run_once()
    assert report["checked_count"] == 1
    assert report["repositories"][0]["trigger"] == "post_publication"
    assert report["repositories"][0]["status"] == "success"
    assert fetcher.calls[-1]["head_sha"] == SHA_A


def test_exact_sha_followup_does_not_accept_a_sha_less_run() -> None:
    clock = MutableClock()
    cp = FakeControlPlane(clock, [project()])
    sha_less = run(clock=clock)
    sha_less["head_sha"] = ""
    fetcher = SequenceFetcher(payload(sha_less))
    service = monitor(cp, clock, fetcher)
    service.schedule_publication_followup(
        task_id="source-task",
        publication_id="publication-1",
        project="widgets",
        canonical_sha=SHA_A,
        published_at=(clock() - timedelta(hours=2)).isoformat(),
    )

    result = service.run_once()["repositories"][0]
    assert result["status"] == "pending"
    assert result["run_count"] == 0


@pytest.mark.parametrize(
    ("duration_hours", "not_due_hours", "due_hours"),
    [(1, 3.9, 0.1), (3, 7.9, 0.1)],
)
def test_periodic_cadence_is_four_or_eight_hours(
    duration_hours: float, not_due_hours: float, due_hours: float
) -> None:
    clock = MutableClock()
    cp = FakeControlPlane(clock, [project()])
    fetcher = SequenceFetcher(
        payload(run(clock=clock, duration_hours=duration_hours)),
        payload(run(clock=clock, duration_hours=duration_hours)),
    )
    service = monitor(cp, clock, fetcher)

    assert service.run_once()["checked_count"] == 1
    clock.advance(hours=not_due_hours)
    assert service.run_once()["checked_count"] == 0
    clock.advance(hours=due_hours)
    assert service.run_once()["checked_count"] == 1


def test_pending_runs_are_rechecked_after_retry_interval() -> None:
    clock = MutableClock()
    cp = FakeControlPlane(clock, [project()])
    fetcher = SequenceFetcher(
        payload(run(clock=clock, status="in_progress", conclusion="")),
        payload(run(clock=clock)),
    )
    service = monitor(cp, clock, fetcher)

    first = service.run_once()
    assert first["pending_count"] == 1
    assert service.run_once()["checked_count"] == 0
    clock.advance(minutes=30)
    second = service.run_once()
    assert second["repositories"][0]["status"] == "success"


def test_repository_without_ci_is_quiet_until_absent_recheck() -> None:
    clock = MutableClock()
    cp = FakeControlPlane(clock, [project()])
    fetcher = SequenceFetcher(
        payload(workflows=False),
        payload(workflows=False),
    )
    service = monitor(cp, clock, fetcher)

    result = service.run_once()["repositories"][0]
    assert result["status"] == "ci_absent"
    assert cp.tasks == []
    clock.advance(hours=23)
    assert service.run_once()["checked_count"] == 0
    clock.advance(hours=1)
    assert service.run_once()["checked_count"] == 1
    assert cp.tasks == []


def test_failures_coalesce_into_one_background_cleanup_per_repository() -> None:
    clock = MutableClock()
    cp = FakeControlPlane(clock, [project()])
    fetcher = SequenceFetcher(
        payload(run(clock=clock, sha=SHA_A, conclusion="failure")),
        payload(run(clock=clock, sha=SHA_A, conclusion="failure")),
        payload(run(clock=clock, sha=SHA_B, conclusion="failure")),
    )
    service = monitor(cp, clock, fetcher)

    first = service.run_once()
    assert first["failure_count"] == 1
    assert first["created_task_count"] == 1
    assert len(cp.tasks) == 1
    task = cp.tasks[0]
    assert task.title == "Reconcile CI health for acme/widgets"
    assert task.kwargs["priority"] == -10
    assert task.metadata["origin"]["type"] == "cicd_cleanup"
    assert task.metadata["origin"]["canonical_sha"] == SHA_A
    assert task.metadata["maintenance"]["blocking"] is False

    clock.advance(hours=4)
    same_failure = service.run_once()
    assert same_failure["created_task_count"] == 0
    assert len(cp.tasks) == 1

    clock.advance(hours=4)
    new_sha = service.run_once()
    assert new_sha["created_task_count"] == 0
    assert len(cp.tasks) == 1
    assert new_sha["repositories"][0]["canonical_sha"] == SHA_B


def test_one_repository_failure_does_not_stop_other_checks() -> None:
    clock = MutableClock()
    cp = FakeControlPlane(
        clock,
        [
            project("widgets"),
            project("gadgets", repository_url="https://github.com/acme/gadgets"),
        ],
    )

    def fetcher(owner: str, repo: str, **kwargs: object) -> dict:
        del owner, kwargs
        if repo == "widgets":
            raise RuntimeError("temporary API failure")
        return payload(run(clock=clock))

    service = CICDMonitor(
        cp,
        CICDMonitorConfig(initial_delay_seconds=0),
        actions_fetcher=fetcher,
        token_provider=lambda: "",
        now=clock,
        reconciliation=FakeReconciliation(),
    )
    report = service.run_once()
    assert report["checked_count"] == 2
    assert {item["status"] for item in report["repositories"]} == {
        "error",
        "success",
    }


def test_run_reports_busy_when_reconciliation_lease_is_held() -> None:
    clock = MutableClock()
    cp = FakeControlPlane(clock, [project()])
    reconciliation = FakeReconciliation()
    reconciliation.next_claim = None
    service = CICDMonitor(
        cp,
        CICDMonitorConfig(initial_delay_seconds=0),
        actions_fetcher=SequenceFetcher(),
        token_provider=lambda: "",
        now=clock,
        reconciliation=reconciliation,
    )

    report = service.run_once()
    assert report["status"] == "busy"
    assert report["reason"] == "reconciler_leased"


def test_schedule_rejects_invalid_sha_and_respects_project_opt_out() -> None:
    clock = MutableClock()
    cp = FakeControlPlane(clock, [project(cicd_monitor_policy=False)])
    service = monitor(cp, clock, SequenceFetcher())

    with pytest.raises(ValueError, match="40-character Git SHA"):
        service.schedule_publication_followup(
            task_id="source-task",
            publication_id="publication-1",
            project="widgets",
            canonical_sha="not-a-sha",
        )
    disabled = service.schedule_publication_followup(
        task_id="source-task",
        publication_id="publication-1",
        project="widgets",
        canonical_sha=SHA_A,
    )
    assert disabled["status"] == "disabled"
    assert cp.events == []
