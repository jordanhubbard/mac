"""Exercise the deployment and worker paths that publish runtime identity."""

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from mac import deploy_env, worker
from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.services import ControlPlane


@pytest.mark.parametrize(
    "configured,expected",
    [
        ("python,openclaw,custom", "python,hermes,custom"),
        ("python,hermes,openclaw,custom", "python,hermes,custom"),
        ("python,custom", "python,custom"),
        ("api,testing,docs,openclaw", "api,testing,docs,hermes"),
    ],
)
def test_capability_projection_preserves_custom_and_useful_capabilities(configured, expected):
    assert deploy_env.normalize_worker_capabilities(configured) == expected
    script = (Path(__file__).resolve().parents[1] / "deploy/deploy-mac-fleet.sh").read_text()
    source = script[script.index("def text_field(") : script.index("def model_field(")]
    namespace = {"Any": Any}
    exec(compile(source, "fleet-capability-projection", "exec"), namespace)
    assert namespace["worker_capabilities_field"](configured) == expected
    assert namespace["worker_capabilities_field"](configured.split(",")) == expected


def test_default_keeps_all_nonruntime_capabilities():
    expected = {
        "ops",
        "python",
        "hermes",
        "review",
        "api",
        "architecture",
        "cli",
        "docs",
        "security",
        "testing",
        "typescript",
        "ui",
        "web_search",
        "web_extract",
        "web_crawl",
        "firecrawl",
    }
    assert set(deploy_env.normalize_worker_capabilities("").split(",")) == expected
    assert (
        set(
            deploy_env.normalize_worker_capabilities(
                ",".join("openclaw" if item == "hermes" else item for item in expected)
            ).split(",")
        )
        == expected
    )


def _resources(implementation="openclaw"):
    return {
        "hardware": {"os": "linux", "cpu_count": 4},
        "media_routes": [{"model": "existing-model"}],
        "representation": {"identity": "worker", "human_facing": True},
        "custom": {"tool": "preserved"},
        "openclaw_runtime": {"ready": True},
        "chat_gateway": {"implementation": implementation, "verified": True},
        "gateway_ownership": {"owner": implementation, "exclusive": True},
    }


@pytest.fixture
def registry(monkeypatch):
    cp = ControlPlane.in_memory()
    client = TestClient(create_app(control_plane=cp))

    def transport(method, path, payload):
        response = (
            client.request(method, path, json=payload)
            if payload is not None
            else client.request(method, path)
        )
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json() if response.content else None

    import mac.hardware

    monkeypatch.setattr(mac.hardware, "detect_hardware", lambda: _resources()["hardware"])
    monkeypatch.setattr(
        worker, "_resources_with_command_inventory", lambda resources, **_: dict(resources)
    )
    monkeypatch.setattr(worker, "_read_only_report_executor_attestation", lambda *_: None)
    monkeypatch.setattr(worker, "_ensure_worker_fleet_membership", lambda *_, **__: None)
    monkeypatch.delenv("MAC_WORKER_DEPLOY_GENERATION", raising=False)
    monkeypatch.delenv("MAC_AGENT_MEDIA_ROUTES", raising=False)
    return cp, MacApiClient("http://mac.test", transport=transport)


@pytest.mark.parametrize("implementation", ["hermes", "none"])
def test_real_registration_withdraws_stale_gateway_without_losing_identity(
    registry, monkeypatch, tmp_path, implementation
):
    cp, api = registry
    monkeypatch.setenv("MAC_CHAT_GATEWAY_IMPL", implementation)
    home = tmp_path / ".mac" / "openclaw"
    home.mkdir(parents=True)
    memory = home / "MEMORY.md"
    memory.write_text("active Hermes memory\n")
    monkeypatch.setenv("HERMES_HOME", str(home))
    resources = _resources()
    original = deepcopy(resources)
    registered = worker.register_worker(
        api,
        hostname="host",
        agent_name="worker",
        resources=resources,
        capabilities=["python", "openclaw", "api", "testing", "custom"],
    )
    stored = cp.get_agent(registered["id"])
    assert set(stored.capabilities) == {"python", "hermes", "api", "testing", "custom"}
    assert not {"openclaw_runtime", "chat_gateway", "gateway_ownership"} & stored.resources.keys()
    for key in ("hardware", "media_routes", "representation", "custom"):
        assert stored.resources[key] == original[key]
    assert resources == original
    assert memory.read_text() == "active Hermes memory\n"


@pytest.mark.parametrize("implementation", ["hermes", "none"])
def test_registration_keeps_current_gateway_only_on_conversational_worker(
    registry, monkeypatch, implementation
):
    cp, api = registry
    monkeypatch.setenv("MAC_CHAT_GATEWAY_IMPL", implementation)
    resources = _resources("hermes")
    registered = worker.register_worker(
        api, hostname="host", agent_name="worker", resources=resources
    )
    stored = cp.get_agent(registered["id"])
    assert stored.capabilities == []
    assert "openclaw_runtime" not in stored.resources
    for key in ("chat_gateway", "gateway_ownership"):
        if implementation == "hermes":
            assert stored.resources[key] == resources[key]
        else:
            assert key not in stored.resources


@pytest.mark.parametrize("read_fails", [False, True])
def test_heartbeat_withdraws_stale_gateway_only_after_reading_complete_resources(
    registry, monkeypatch, tmp_path, read_fails
):
    cp, api = registry
    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "worker", resources=_resources())
    current = worker.MacWorker(api, agent.id, tmp_path, lambda *_: worker.WorkerExecution(0, "ok"))
    monkeypatch.setenv("MAC_CHAT_GATEWAY_IMPL", "hermes")
    monkeypatch.setattr(current, "_maybe_start_coding_route_probe", lambda: None)
    monkeypatch.setattr(current, "_maybe_command_inventory_resources", lambda: None)
    before = deepcopy(cp.get_agent(agent.id).resources)
    if read_fails:
        monkeypatch.setattr(
            api, "get", lambda *_: (_ for _ in ()).throw(MacApiError("unavailable"))
        )
    current._heartbeat()
    after = cp.get_agent(agent.id).resources
    if read_fails:
        assert after == before
    else:
        assert not {"openclaw_runtime", "chat_gateway", "gateway_ownership"} & after.keys()
        for key in ("hardware", "media_routes", "representation", "custom"):
            assert after[key] == before[key]
