"""_advance_review_workflow_after_verdict must not log-spam on repeat failure.

Before every worker credential carried the `review:advance` scope, this call
failed on EVERY recorded verdict fleet-wide with the same permanent
rejection ("token lacks required scope: admin") -- a warning log per verdict,
forever. Now that the scope gap is closed a failure here is a real signal
again, but it must still be rate-limited: a genuine outage would otherwise
still log once per verdict across the whole fleet.
"""

from __future__ import annotations

from pathlib import Path

from mac import worker


class _FailingClient:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def post(self, path: str, payload: dict):
        self.posts.append(path)
        raise worker.MacApiError("token lacks required scope: admin")


def _instance(tmp_path: Path) -> worker.MacWorker:
    return worker.MacWorker(
        _FailingClient(),
        "agent_x",
        tmp_path / "workspace",
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


def test_repeated_failures_within_the_window_log_once(tmp_path, monkeypatch):
    instance = _instance(tmp_path)
    logged: list[dict] = []
    monkeypatch.setattr(
        instance,
        "_observe_log",
        lambda name, **kwargs: logged.append({"name": name, **kwargs}),
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock["t"])

    instance._advance_review_workflow_after_verdict("task_1")
    clock["t"] += 5.0
    instance._advance_review_workflow_after_verdict("task_2")
    clock["t"] += 5.0
    instance._advance_review_workflow_after_verdict("task_3")

    assert len(instance.client.posts) == 3  # every call still attempted
    assert len(logged) == 1  # only the first failure was logged
    assert logged[0]["name"] == "worker.review_workflow.advance_failed"


def test_a_failure_outside_the_window_logs_again(tmp_path, monkeypatch):
    instance = _instance(tmp_path)
    logged: list[dict] = []
    monkeypatch.setattr(
        instance,
        "_observe_log",
        lambda name, **kwargs: logged.append({"name": name, **kwargs}),
    )
    clock = {"t": 1000.0}
    monkeypatch.setattr(worker.time, "monotonic", lambda: clock["t"])

    instance._advance_review_workflow_after_verdict("task_1")
    clock["t"] += 301.0
    instance._advance_review_workflow_after_verdict("task_2")

    assert len(logged) == 2
