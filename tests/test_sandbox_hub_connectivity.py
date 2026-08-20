"""Hub-write capability probe used to skip impossible planning phases."""

from __future__ import annotations

import json
from pathlib import Path

from mac.executor_hub_io import hub_write_capability
from mac.executor_scope import reject_empty_plan_decomposed_evidence
from mac.worker import _plan_decomposed_is_environment_fault


def test_hub_write_capability_missing_env(monkeypatch) -> None:
    for name in (
        "MAC_HUB_URL",
        "MAC_URL",
        "MAC_WORKER_TOKEN",
        "MAC_TOKEN",
        "MAC_API_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    result = hub_write_capability(probe=False)
    assert result["ready"] is False
    assert result["reason"] == "hub_url_and_credentials_missing"
    assert result["schema"] == "mac.sandbox_hub_connectivity.v1"


def test_hub_write_capability_missing_token(monkeypatch) -> None:
    monkeypatch.setenv("MAC_HUB_URL", "http://hub.example.test:8789")
    for name in ("MAC_WORKER_TOKEN", "MAC_TOKEN", "MAC_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    result = hub_write_capability(probe=False)
    assert result["ready"] is False
    assert result["reason"] == "hub_credentials_missing"
    assert result["has_url"] is True
    assert result["loopback_url"] is False


def test_hub_write_capability_unreachable(monkeypatch) -> None:
    monkeypatch.setenv("MAC_HUB_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "token")
    result = hub_write_capability(probe=True, timeout=0.2)
    assert result["ready"] is False
    assert result["reason"] == "hub_unreachable"
    assert result["reachable"] is False
    assert result["loopback_url"] is True


def test_reject_empty_plan_decomposed_rewrites_manifest(tmp_path: Path) -> None:
    path = tmp_path / "mac-evidence.json"
    path.write_text(
        json.dumps(
            {
                "evidence_type": "plan_decomposed",
                "status": "complete",
                "children": [],
            }
        ),
        encoding="utf-8",
    )
    assert reject_empty_plan_decomposed_evidence(tmp_path) is True
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["evidence_type"] != "plan_decomposed"
    assert loaded["rejected_evidence_type"] == "plan_decomposed"
    assert loaded["status"] == "invalid"


def test_reject_empty_plan_leaves_routable_plans(tmp_path: Path) -> None:
    path = tmp_path / "mac-evidence.json"
    path.write_text(
        json.dumps(
            {
                "evidence_type": "plan_decomposed",
                "children": [{"title": "Child A"}, {"title": "Child B"}],
            }
        ),
        encoding="utf-8",
    )
    assert reject_empty_plan_decomposed_evidence(tmp_path) is False
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert loaded["evidence_type"] == "plan_decomposed"


def test_empty_children_are_an_environment_fault() -> None:
    assert _plan_decomposed_is_environment_fault(
        {},
        ["plan_decomposed evidence requires a non-empty children list"],
    )


def test_unroutable_children_are_an_environment_fault() -> None:
    assert _plan_decomposed_is_environment_fault(
        {},
        ["plan_decomposed evidence could not be routed to durable child tasks: 401"],
    )


def test_recorded_hub_probe_failure_is_an_environment_fault(tmp_path: Path) -> None:
    (tmp_path / "sandbox-hub-connectivity.json").write_text(
        json.dumps({"ready": False, "reason": "hub_credentials_missing"}),
        encoding="utf-8",
    )
    assert _plan_decomposed_is_environment_fault({}, ["unrelated"], tmp_path)
