"""The claim path triages before it works, and routes what it decides.

Triage is wired into ``execute_assignment`` between workspace preparation and
the coding agent, so it reads the canonical head the task targets and stops the
assignment before any work is done. These tests drive the two worker methods
directly against a real git checkout, with the hub calls recorded.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from mac.task_triage import TriageAction, TriageOutcome, TriageReason
from mac.worker import MacWorker

TASK_ID = "task_9f3b80b8c1d24e0f"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


class _FakeClient:
    """Records hub calls; ``fail_on`` makes one verb refuse, as a hub that
    declines a worker's privilege would."""

    def __init__(self, fail_on: str = "") -> None:
        self.calls: List[Dict[str, Any]] = []
        self.fail_on = fail_on

    def _record(self, verb: str, path: str, payload: Any) -> Any:
        self.calls.append({"verb": verb, "path": path, "payload": payload})
        if self.fail_on == verb:
            raise RuntimeError("hub refused %s %s" % (verb, path))
        return {"id": TASK_ID, "state": "cancelled"}

    def post(self, path: str, payload: Any) -> Any:
        return self._record("post", path, payload)

    def put(self, path: str, payload: Any) -> Any:
        return self._record("put", path, payload)

    def get(self, path: str) -> Any:
        self.calls.append({"verb": "get", "path": path, "payload": None})
        return {"task": {"id": TASK_ID, "state": "open"}}


def _worker(client: _FakeClient) -> MacWorker:
    w = MacWorker.__new__(MacWorker)  # type: ignore[call-arg]
    w.agent_id = "agent_test"
    w.client = client  # type: ignore[assignment]
    w.activity: List[Dict[str, Any]] = []  # type: ignore[attr-defined]
    w.observed: List[Dict[str, Any]] = []  # type: ignore[attr-defined]

    def _observe_log(event, level="info", subject_type="", subject_id="", detail=None):
        w.observed.append({"event": event, "level": level, "detail": detail or {}})

    def _observe_metric(name, value, unit="", subject_type="", subject_id="", detail=None):
        w.observed.append({"event": name, "value": value, "detail": detail or {}})

    def _post_task_activity(task_id, phase, summary, lease_id=None):
        w.activity.append({"phase": phase, "summary": summary})

    w._observe_log = _observe_log  # type: ignore[method-assign]
    w._observe_metric = _observe_metric  # type: ignore[method-assign]
    w._post_task_activity = _post_task_activity  # type: ignore[method-assign]
    return w


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "canonical"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "triage@example.invalid")
    _git(root, "config", "user.name", "Triage Test")
    (root / "docs").mkdir()
    (root / "docs" / "adr.md").write_text(
        "ADR quoting %s as motivating evidence.\n" % TASK_ID, encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "ADR citing %s as evidence" % TASK_ID)
    return root


def _task(repo: Path, *, scope_paths, markers=(), description="do the thing"):
    head = _git(repo, "rev-parse", "HEAD")
    return {
        "id": TASK_ID,
        "title": "the thing",
        "description": description,
        "metadata": {
            "triage": {
                "scope_paths": list(scope_paths),
                "acceptance_markers": list(markers),
            },
            "runtime": {
                "repository_worktree": str(repo),
                "repository_base_sha": head,
                "repository_canonical_branch": "main",
                "repository_canonical_remote": "github.com/example/mac",
            },
        },
    }


LEASE = {"id": "lease_test"}


def _land_the_work(repo: Path) -> str:
    src = repo / "src" / "mac"
    src.mkdir(parents=True)
    (src / "widget.py").write_text("def build_widget():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add the widget")
    return _git(repo, "rev-parse", "HEAD")


def test_a_task_with_no_repository_is_not_triaged():
    w = _worker(_FakeClient())
    assert w._triage_before_work({"id": TASK_ID, "metadata": {}}, LEASE) is None


def test_every_verdict_is_recorded_on_the_task_including_the_ones_that_proceed(
    repo: Path,
):
    """A CANNOT_TELL that proceeds silently is indistinguishable from no triage."""
    w = _worker(_FakeClient())
    task = _task(repo, scope_paths=[], markers=[], description="prose only")
    decision = w._triage_before_work(task, LEASE)

    assert decision is not None
    assert decision.outcome == TriageOutcome.CANNOT_TELL
    assert decision.proceeds is True
    assert [entry["phase"] for entry in w.activity] == ["triage"]
    assert "cannot_tell" in w.activity[0]["summary"]
    decided = [e for e in w.observed if e["event"] == "worker.triage.decided"]
    assert decided and decided[0]["detail"]["outcome"] == TriageOutcome.CANNOT_TELL
    assert any(e["event"] == "worker.triage.cost_ms" for e in w.observed)


def test_the_cost_of_triage_is_bounded_and_reported(repo: Path):
    w = _worker(_FakeClient())
    task = _task(repo, scope_paths=["docs/adr.md"], markers=["motivating evidence"])
    decision = w._triage_before_work(task, LEASE)

    assert decision is not None
    cost = decision.evidence.cost
    assert cost.budget_exhausted is False
    # A handful of queries -- triage is a read, not a second execution.
    assert cost.git_calls <= 12
    payload = [e for e in w.observed if e["event"] == "worker.triage.decided"][0]
    assert payload["detail"]["cost"]["git_calls"] == cost.git_calls


def test_a_change_that_only_mentions_the_task_does_not_close_the_assignment(
    repo: Path,
):
    """The 24/24 false positive, at the claim path: the head has a commit
    naming the task id and nothing that carries the work."""
    w = _worker(_FakeClient())
    task = _task(
        repo,
        scope_paths=["docs/adr.md"],
        markers=["def build_widget("],
    )
    decision = w._triage_before_work(task, LEASE)

    assert decision is not None
    assert decision.outcome == TriageOutcome.STILL_NEEDED
    assert decision.action == TriageAction.PROCEED
    assert w._route_triage_decision(task, LEASE, decision) is None
    # Nothing was transitioned; the assignment proceeds to do the work.
    assert [c for c in w.client.calls if c["verb"] in {"post", "put"}] == []


def test_landed_work_cancels_the_assignment_and_cites_the_commit(repo: Path):
    head = _land_the_work(repo)
    client = _FakeClient()
    w = _worker(client)
    task = _task(
        repo, scope_paths=["src/mac/widget.py"], markers=["def build_widget("]
    )
    decision = w._triage_before_work(task, LEASE)
    assert decision is not None
    assert decision.outcome == TriageOutcome.ALREADY_LANDED

    result = w._route_triage_decision(task, LEASE, decision)
    assert result is not None
    assert result.status == "cancelled"

    transition = [c for c in client.calls if c["path"].endswith("/transition")][0]
    assert transition["payload"]["target_state"] == "cancelled"
    detail = transition["payload"]["detail"]
    assert detail["reason"] == "triage_already_landed"
    # The specific commit, not merely one that mentions the task.
    assert detail["citation"]["ref"] == head
    assert detail["citation"]["relation"] == "implements_scope"
    assert head in detail["output"]
    # And the close is auditable from the task narrative alone.
    assert any(head in entry["summary"] for entry in w.activity)


def test_a_hub_that_refuses_the_close_leaves_the_work_to_be_done(repo: Path):
    _land_the_work(repo)
    client = _FakeClient(fail_on="post")
    w = _worker(client)
    task = _task(
        repo, scope_paths=["src/mac/widget.py"], markers=["def build_widget("]
    )
    decision = w._triage_before_work(task, LEASE)
    assert decision is not None and decision.action == TriageAction.CLOSE
    # Routing returns None -> execute_assignment falls through and works it.
    assert w._route_triage_decision(task, LEASE, decision) is None
    assert any(e["event"] == "worker.triage.close_failed" for e in w.observed)


def test_a_stale_scope_is_corrected_atomically_and_re_entered_from_the_top(
    repo: Path,
):
    client = _FakeClient()
    w = _worker(client)
    task = _task(repo, scope_paths=["docs/adr.md", "src/mac/gone.py"])
    decision = w._triage_before_work(task, LEASE)
    assert decision is not None
    assert decision.outcome == TriageOutcome.SCOPE_STALE
    assert decision.reason == TriageReason.SCOPE_PATHS_MISSING

    result = w._route_triage_decision(task, LEASE, decision)
    assert result is not None

    update = [c for c in client.calls if c["verb"] == "put"][0]
    assert update["path"] == "/tasks/%s" % TASK_ID
    hints = update["payload"]["metadata"]["triage"]
    assert hints["scope_paths"] == ["docs/adr.md"]
    assert hints["removed_scope_paths"] == ["src/mac/gone.py"]
    assert update["payload"]["metadata"]["triage_verdict"]["reason"] == (
        TriageReason.SCOPE_PATHS_MISSING
    )
    # ADR 0020 restarts the task, so this assignment is no longer current.
    assert result.status != "cancelled"
    assert json.dumps(update["payload"])  # payload is JSON-serialisable for the hub


def test_a_declined_scope_update_proceeds_against_the_task_as_filed(repo: Path):
    client = _FakeClient(fail_on="put")
    w = _worker(client)
    task = _task(repo, scope_paths=["docs/adr.md", "src/mac/gone.py"])
    decision = w._triage_before_work(task, LEASE)
    assert decision is not None and decision.action == TriageAction.UPDATE_SCOPE

    assert w._route_triage_decision(task, LEASE, decision) is None
    assert any(
        e["event"] == "worker.triage.scope_update_declined" for e in w.observed
    )
    assert any("declined" in entry["summary"] for entry in w.activity)


def test_a_broken_checkout_does_not_block_the_assignment(tmp_path: Path):
    w = _worker(_FakeClient())
    task = {
        "id": TASK_ID,
        "description": "do the thing",
        "metadata": {
            "runtime": {"repository_worktree": str(tmp_path / "missing")},
            "triage": {"scope_paths": ["src/mac/widget.py"]},
        },
    }
    decision = w._triage_before_work(task, LEASE)
    assert decision is not None
    assert decision.proceeds is True
    assert decision.reason == TriageReason.NO_HEAD_EVIDENCE
