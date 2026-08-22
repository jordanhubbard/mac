"""Hub-write capability probe used to skip impossible planning phases."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from mac import task_executor as te
from mac.executor_hub_io import hub_write_capability
from mac.executor_scope import reject_empty_plan_decomposed_evidence
from mac.worker import _plan_decomposed_is_environment_fault

_REPO_ROOT = Path(__file__).resolve().parents[1]
_OPERATOR_POLICY = _REPO_ROOT / "deploy" / "openshell" / "mac-hermes-policy.yaml"

# The bearers the host executor holds and the model sandbox must never see.
# Duplicated literally rather than imported so that narrowing the production
# frozenset is a test failure instead of a silently-agreeing tautology.
_EXPECTED_HOST_ONLY_HUB_CREDENTIALS = {
    "MAC_WORKER_TOKEN",
    "MAC_TOKEN",
    "MAC_API_TOKEN",
    "MAC_ATTESTATION_KEY",
    "MAC_HUB_TOKEN",
}


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


# ---------------------------------------------------------------------------
# The credential boundary the planning-phase skip exists to preserve.
#
# Skipping an impossible hub-write phase is only the right fix while the
# sandbox stays denied hub authority. Widening the boundary would "fix" the
# same throughput symptom by handing a fleet worker bearer to model-authored
# code, so these tests pin the boundary itself: the strip is load-bearing
# because the default passthrough list names three of these bearers.
# ---------------------------------------------------------------------------


def _sandbox_env(monkeypatch, **overrides: str) -> dict:
    """Build the sandbox environment file contents from a controlled host env."""
    monkeypatch.delenv("MAC_TASK_REPO_ACCESS_MODE", raising=False)
    monkeypatch.delenv("MAC_TASK_REPO_ACCESS_SCHEMA", raising=False)
    for name, value in overrides.items():
        monkeypatch.setenv(name, value)
    return te._openshell_environment()


def test_host_only_hub_credentials_are_never_narrowed() -> None:
    assert set(te._HOST_ONLY_HUB_CREDENTIALS) == _EXPECTED_HOST_ONLY_HUB_CREDENTIALS


def test_host_only_hub_credentials_are_stripped_from_the_sandbox(monkeypatch) -> None:
    monkeypatch.delenv("MAC_OPENSHELL_ENV_PASSTHROUGH", raising=False)
    values = _sandbox_env(
        monkeypatch,
        MAC_HUB_URL="http://100.72.16.110:8789",
        **{name: "secret-%s" % name for name in _EXPECTED_HOST_ONLY_HUB_CREDENTIALS},
    )
    serialized = json.dumps(values)
    for name in _EXPECTED_HOST_ONLY_HUB_CREDENTIALS:
        assert name not in values, "%s reached the sandbox environment" % name
        # Not just the key: the bearer must not survive under another name.
        assert "secret-%s" % name not in serialized
    # The URL is not a credential: the sandbox may know where the hub is.
    assert values["MAC_HUB_URL"] == "http://100.72.16.110:8789"


def test_default_passthrough_alone_would_leak_without_the_strip() -> None:
    """The strip is not belt-and-braces — the default list names these bearers."""
    default_names = {
        item.strip()
        for item in te._DEFAULT_OPENSHELL_ENV_PASSTHROUGH.split(",")
        if item.strip()
    }
    assert default_names & _EXPECTED_HOST_ONLY_HUB_CREDENTIALS


def test_custom_passthrough_cannot_reintroduce_a_hub_credential(monkeypatch) -> None:
    values = _sandbox_env(
        monkeypatch,
        MAC_OPENSHELL_ENV_PASSTHROUGH=",".join(
            sorted(_EXPECTED_HOST_ONLY_HUB_CREDENTIALS)
        ),
        **{name: "secret-%s" % name for name in _EXPECTED_HOST_ONLY_HUB_CREDENTIALS},
    )
    assert values == {}


def test_operator_policy_hub_egress_is_one_templated_host() -> None:
    """No literal hub address may be added — the fleet substitutes the template."""
    policy = yaml.safe_load(_OPERATOR_POLICY.read_text(encoding="utf-8"))
    endpoints = policy["network_policies"]["mac_hub"]["endpoints"]
    assert [endpoint["host"] for endpoint in endpoints] == ["__MAC_HUB_HOST__"]
    assert [str(endpoint["port"]) for endpoint in endpoints] == ["__MAC_HUB_PORT__"]
    assert endpoints[0]["enforcement"] == "enforce"


def test_loopback_hub_url_is_reported_even_when_credentials_are_present(
    monkeypatch,
) -> None:
    """The sandbox reached for localhost:8789 while the hub was elsewhere.

    A loopback URL inside the sandbox is that sandbox's own loopback, so the
    capability record has to name the condition even when nothing else is
    missing.
    """
    monkeypatch.setenv("MAC_HUB_URL", "http://localhost:8789")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "token")
    result = hub_write_capability(probe=False)
    assert result["loopback_url"] is True
    assert result["url_host"] == "localhost:8789"


def test_routable_hub_url_is_not_flagged_as_loopback(monkeypatch) -> None:
    monkeypatch.setenv("MAC_HUB_URL", "http://100.72.16.110:8789")
    monkeypatch.setenv("MAC_WORKER_TOKEN", "token")
    result = hub_write_capability(probe=False)
    assert result["loopback_url"] is False
    assert result["reason"] == "ready"
