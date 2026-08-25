from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_runtime import build_runtime_context, render_runtime_markdown
import mac.hermes_startup as hermes_startup
from mac.hermes_startup import build_hermes_startup_report
from mac.models import ValidationError
from mac.services import ControlPlane

# imports relocated from test_hermes_startup_edges.py
from pathlib import Path
from mac import hermes_startup as startup


def _write(path, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _clear_startup_env(monkeypatch) -> None:
    for name in (
        "ACC_DIR",
        "HERMES_AGENT_DIR",
        "HERMES_REDACT_SECRETS",
        "HERMES_HOME",
        "MAC_HERMES_AGENT_DIR",
        "MAC_HERMES_APPLY_GATEWAY_RUNTIME_SHIM",
        "MAC_HERMES_GATEWAY_BASE_URL",
        "MAC_HERMES_GATEWAY_MODEL",
        "MAC_HERMES_GATEWAY_PROVIDER",
        "MAC_HERMES_LOG_SUMMARY",
        "MAC_HERMES_RUNTIME_CONTEXT_FILE",
        "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN",
        "MAC_HERMES_RUNTIME_CONTEXT_REQUIRED",
        "MAC_HERMES_WORKSPACE",
        "MAC_HERMES_INSTANCE_ID",
        "MAC_HERMES_APPLY_SLACK_ACCOUNT_SHIM",
        "MAC_HERMES_STARTUP_CHECK",
        "MAC_HERMES_SLACK_HOME_CHANNEL_NAME",
        "MAC_FIRECRAWL_ALLOW_DEGRADED",
        "MAC_FIRECRAWL_CHECK_TIMEOUT_SECONDS",
        "MAC_MEMORY_TOPOLOGY_FILE",
        "MAC_QDRANT_CHECK_TIMEOUT_SECONDS",
        "MAC_QDRANT_MEMORY",
        "MAC_QDRANT_MEMORY_ALLOW_DEGRADED",
        "MAC_QDRANT_MEMORY_ROLE",
        # Leads the canonical QDRANT_URL_ENV_NAMES cascade, so the endpoint
        # probe honours it and these tests must not inherit it.
        "MAC_QDRANT_URL",
        "MAC_REQUIRE_HERMES_STARTUP_READY",
        "MAC_REQUIRE_FIRECRAWL",
        "MAC_REQUIRE_QDRANT_MEMORY",
        "MAC_REQUIRE_TOKENHUB",
        "MAC_SHARED_SERVICES_MANAGER_AGENT",
        "MAC_PROJECT_CONTRACT_FILE",
        "MAC_TOKENHUB_ALLOW_DEGRADED",
        "MAC_TOKENHUB_CHECK_TIMEOUT_SECONDS",
        "MAC_TOKENHUB_URL",
        "MAC_URL",
        "MAC_WORKER_HERMES_INSTANCE_ID",
        "ACC_HERMES_GATEWAY_BASE_URL",
        "ACC_HERMES_GATEWAY_MODEL",
        "ACC_HERMES_GATEWAY_PROVIDER",
        "ACC_QDRANT_MEMORY",
        "ACC_QDRANT_MEMORY_ALLOW_DEGRADED",
        "ACC_REQUIRE_QDRANT_MEMORY",
        "ACC_LLM_MODEL",
        "ACC_SLACK_HOME_CHANNEL_NAME",
        "CUSTOM_BASE_URL",
        "FIRECRAWL_API_URL",
        "FIRECRAWL_GATEWAY_URL",
        "HERMES_INFERENCE_MODEL",
        "HERMES_INFERENCE_PROVIDER",
        "MAC_WEB_SEARCH_PROVIDER",
        "MAC_WEB_SEARCH_URL",
        "OPENAI_BASE_URL",
        "QDRANT_ADDRESS",
        "QDRANT_API_KEY",
        "QDRANT_FLEET_KEY",
        "QDRANT_FLEET_URL",
        "QDRANT_URL",
        "SLACK_BOT_TOKEN",
        "SLACK_HOME_CHANNEL_NAME",
        "TOKENHUB_API_KEY",
        "TOKENHUB_AGENT_KEY",
        "TOKENHUB_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _executable(path, content: str = "#!/bin/sh\nexit 0\n") -> None:
    _write(path, content)
    path.chmod(0o755)


def _prepare_direct_session_tools(monkeypatch, mac_home, workspace) -> None:
    for name in ("mac", "mac-hermes", "mac-firecrawl-gateway"):
        _executable(mac_home / "venv" / "bin" / name)
    for name in ("mac-task-executor",):
        _executable(mac_home / "bin" / name)
    _executable(workspace / "scripts" / "run-contract-tests.sh")
    monkeypatch.setenv(
        "PATH",
        "%s:%s:%s"
        % (
            mac_home / "venv" / "bin",
            mac_home / "bin",
            os.environ.get("PATH", ""),
        ),
    )
    monkeypatch.setenv("FIRECRAWL_GATEWAY_URL", "http://hub.example.internal:3002")
    monkeypatch.setattr(
        hermes_startup,
        "_fetch_firecrawl_health",
        lambda endpoint, api_key, timeout_seconds: {"ok": True},
    )


def _configure_mandatory_shared_services(monkeypatch, hermes_home) -> None:
    topology = hermes_home / "mac-memory-topology.json"
    _write(
        topology,
        json.dumps(
            {
                "schema": "mac.hermes.memory_topology.v1",
                "agent": "hub",
                "hub": {"agent": "hub", "url": "http://hub.example.internal:8789"},
                "shared_services": {
                    "qdrant": {
                        "url": "http://hub.example.internal:6333",
                        "role": "shared_level2_memory",
                    },
                    "firecrawl": {
                        "url": "http://hub.example.internal:3002",
                        "role": "shared_web_search",
                    },
                },
            }
        ),
    )
    monkeypatch.setenv("MAC_MEMORY_TOPOLOGY_FILE", str(topology))
    monkeypatch.setenv("MAC_REQUIRE_QDRANT_MEMORY", "1")
    monkeypatch.setenv("QDRANT_URL", "http://hub.example.internal:6333")
    monkeypatch.setenv("MAC_REQUIRE_FIRECRAWL", "1")
    monkeypatch.setenv("FIRECRAWL_GATEWAY_URL", "http://hub.example.internal:3002")
    monkeypatch.setattr(
        hermes_startup,
        "_fetch_qdrant_collections",
        lambda endpoint, api_key, timeout_seconds: {"result": {"collections": []}},
    )
    monkeypatch.setattr(
        hermes_startup,
        "_fetch_firecrawl_health",
        lambda endpoint, api_key, timeout_seconds: {"ok": True},
    )


def test_startup_report_inventories_hermes_state_without_contents(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    acc_dir = tmp_path / ".acc"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "secret soul text")
    _write(hermes_home / "MEMORY.md", "private memory text")
    _write(hermes_home / "state.db", "state bytes")
    _write(hermes_home / "slack_accounts.json", '{"token":"secret-slack-token"}')
    _write(acc_dir / "data" / "fleet.db", "fleet")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ACC_DIR", str(acc_dir))

    report = build_hermes_startup_report()

    assert report["enabled"] is True
    assert report["checks"]["soul_present"] is True
    assert report["checks"]["conversation_state_present"] is True
    # ADR 0001 hu-04: multi-slack support is baked into the vendored gateway, so
    # an account file alone activates Slack — no upstream-checkout shim needed.
    assert report["slack"]["needs_account_file_activation_shim"] is False
    assert report["slack"]["activation_source"] == "slack_accounts_file"
    roles = {ref["role"] for ref in report["state_refs"] if ref["exists"]}
    assert {"soul", "long_term_memory", "conversation_state", "slack_accounts"} <= roles
    rendered = str(report)
    assert "secret soul text" not in rendered
    assert "private memory text" not in rendered
    assert "secret-slack-token" not in rendered


def test_startup_report_treats_unwritten_hermes_memory_as_pending_not_warning(
    monkeypatch, tmp_path
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_REQUIRE_QDRANT_MEMORY", "0")
    monkeypatch.setenv("MAC_REQUIRE_TOKENHUB", "0")

    report = build_hermes_startup_report()

    assert report["checks"]["long_term_memory_present"] is False
    assert "Hermes MEMORY.md is missing" not in " ".join(report["warnings"])


def test_startup_reports_tokenhub_readiness_without_leaking_key(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        agent_dir / "gateway" / "run.py",
        "MAC_HERMES_GATEWAY_MODEL = True\nMAC_HERMES_GATEWAY_PROVIDER = True\nresolve_runtime_provider = True\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("TOKENHUB_URL", "http://tokenhub.internal:8090")
    monkeypatch.setenv("TOKENHUB_API_KEY", "secret-tokenhub-key")
    monkeypatch.setenv("MAC_REQUIRE_TOKENHUB", "1")
    _configure_mandatory_shared_services(monkeypatch, hermes_home)

    def fake_fetch(endpoint, path, api_key, timeout_seconds):
        assert endpoint == "http://tokenhub.internal:8090"
        if path == "/healthz":
            assert api_key is None
            return {"status": "ok", "adapters": 2}
        assert path == "/v1/models"
        assert api_key == "secret-tokenhub-key"
        return {"data": [{"id": "model-a"}, {"id": "model-b"}]}

    monkeypatch.setattr(hermes_startup, "_fetch_tokenhub_json", fake_fetch)

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["tokenhub"]["status"] == "ready"
    assert report["tokenhub"]["model_count"] == 2
    assert report["tokenhub"]["adapter_count"] == 2
    assert report["checks"]["tokenhub_ready"] is True
    assert report["operator_health"]["tokenhub_model_count"] == 2
    assert "secret-tokenhub-key" not in str(report)


def test_spoke_router_v1_not_mistaken_for_tokenhub(monkeypatch):
    # th-merge-07 / Stream B: a spoke's OPENAI_BASE_URL is the HUB's router /v1, not
    # a TokenHub. The startup report must NOT health-check it as a TokenHub (doing so
    # produced a spurious "403 Forbidden" + degraded operator status). With no
    # explicit TOKENHUB_URL the report is "retired", ready, no warning.
    for var in (
        "TOKENHUB_URL",
        "MAC_TOKENHUB_URL",
        "MAC_ROUTER_BACKEND",
        "MAC_REQUIRE_TOKENHUB",
        "CUSTOM_BASE_URL",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("OPENAI_BASE_URL", "http://100.125.137.89:8789/v1")
    assert hermes_startup._tokenhub_endpoint_from_env() == (None, None)

    def _boom(*a, **k):
        raise AssertionError("must not health-check a TokenHub endpoint on a spoke")

    monkeypatch.setattr(hermes_startup, "_fetch_tokenhub_json", _boom)
    report = hermes_startup._tokenhub_report()
    assert report["status"] == "retired"
    assert report["ready"] is True
    assert not report["warning"]


def test_slack_bot_token_satisfies_upstream_slack_activation(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(hermes_home / "slack_accounts.json", '{"workspace":"T123"}')
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-not-returned")
    _configure_mandatory_shared_services(monkeypatch, hermes_home)

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["slack"]["activation_source"] == "slack_bot_token"
    assert "xoxb-not-returned" not in str(report)


def test_startup_fails_readiness_when_secret_redaction_is_disabled(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "false")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["security"]["secret_redaction"]["effective"] is False
    assert report["checks"]["secret_redaction_enabled"] is False
    assert "secret redaction is disabled" in " ".join(report["warnings"])


def test_startup_detects_inherited_env_file_redaction_drift(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    acc_dir = tmp_path / ".acc"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(hermes_home / ".env", "HERMES_REDACT_SECRETS=false\nSECRET=not-returned")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ACC_DIR", str(acc_dir))
    monkeypatch.setenv("HERMES_REDACT_SECRETS", "true")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["security"]["secret_redaction"]["effective"] is True
    assert report["security"]["secret_redaction"]["drift_detected"] is True
    assert report["security"]["secret_redaction"]["env_files"][0]["redact_secrets"] == "false"
    assert "not-returned" not in str(report)


def test_qdrant_shared_memory_missing_endpoint_blocks_readiness(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["qdrant_level2"]["required"] is True
    assert report["qdrant_level2"]["mandatory"] is True
    assert report["qdrant_level2"]["status"] == "missing_endpoint"
    assert report["checks"]["shared_qdrant_memory_ready"] is False


def test_qdrant_endpoint_probe_honours_mac_qdrant_url(monkeypatch, tmp_path):
    """MAC_QDRANT_URL leads the canonical cascade the hub and the vector
    writer resolve through, but this probe used to skip it — so a fleet
    configured only that way was reported "missing_endpoint" while memory
    was working. A probe that disagrees with the thing it probes is worse
    than no probe: it sends the operator looking for an outage that isn't."""
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_QDRANT_URL", "http://hub.example.internal:6333")
    monkeypatch.setattr(
        hermes_startup,
        "_fetch_qdrant_collections",
        lambda endpoint, api_key, timeout_seconds: {"result": {"collections": []}},
    )

    report = build_hermes_startup_report()

    assert report["qdrant_level2"]["status"] != "missing_endpoint"
    assert report["qdrant_level2"]["endpoint_source"] == "MAC_QDRANT_URL"


def test_qdrant_endpoint_probe_prefers_mac_qdrant_url_over_qdrant_url(monkeypatch, tmp_path):
    """Precedence matches mac.memory_config.QDRANT_URL_ENV_NAMES exactly."""
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_QDRANT_URL", "http://first.example.internal:6333")
    monkeypatch.setenv("QDRANT_URL", "http://second.example.internal:6333")
    monkeypatch.setattr(
        hermes_startup,
        "_fetch_qdrant_collections",
        lambda endpoint, api_key, timeout_seconds: {"result": {"collections": []}},
    )

    report = build_hermes_startup_report()

    assert report["qdrant_level2"]["endpoint_source"] == "MAC_QDRANT_URL"
    assert "first.example.internal" in report["qdrant_level2"]["endpoint"]


def test_required_qdrant_without_endpoint_blocks_readiness(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_REQUIRE_QDRANT_MEMORY", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["qdrant_level2"]["status"] == "missing_endpoint"
    assert report["checks"]["shared_qdrant_memory_ready"] is False
    assert "required Qdrant shared memory endpoint is not configured" in " ".join(
        report["warnings"]
    )


def test_required_qdrant_endpoint_uses_redacted_topology(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    topology = hermes_home / "mac-memory-topology.json"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        topology,
        json.dumps(
            {
                "schema": "mac.hermes.memory_topology.v1",
                "agent": "hub",
                "hub": {"agent": "hub", "url": "http://secret@hub.example.internal:8789"},
                "shared_services": {
                    "qdrant": {
                        "url": "http://secret@hub.example.internal:6333?token=hidden",
                        "role": "shared_level2_memory",
                    }
                },
            }
        ),
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("QDRANT_URL", "http://secret@hub.example.internal:6333?token=hidden")
    monkeypatch.setenv("QDRANT_API_KEY", "secret-qdrant-key")
    monkeypatch.setenv("MAC_REQUIRE_FIRECRAWL", "1")
    monkeypatch.setenv("FIRECRAWL_GATEWAY_URL", "http://hub.example.internal:3002")
    monkeypatch.setattr(
        hermes_startup,
        "_fetch_firecrawl_health",
        lambda endpoint, api_key, timeout_seconds: {"ok": True},
    )

    def fake_fetch(endpoint, api_key, timeout_seconds):
        assert endpoint == "http://secret@hub.example.internal:6333?token=hidden"
        assert api_key == "secret-qdrant-key"
        assert timeout_seconds == 2
        return {"result": {"collections": [{"name": "hermes-memory"}]}}

    monkeypatch.setattr("mac.hermes_startup._fetch_qdrant_collections", fake_fetch)

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["qdrant_level2"]["status"] == "ready"
    assert report["qdrant_level2"]["collection_count"] == 1
    assert report["qdrant_level2"]["api_key_present"] is True
    assert report["qdrant_level2"]["endpoint"] == "http://redacted@hub.example.internal:6333"
    assert (
        report["qdrant_level2"]["topology"]["hub_url"]
        == "http://redacted@hub.example.internal:8789"
    )
    assert (
        report["qdrant_level2"]["topology"]["qdrant_url"]
        == "http://redacted@hub.example.internal:6333"
    )
    rendered = str(report)
    assert "secret-qdrant-key" not in rendered
    assert "token=hidden" not in rendered


def test_required_firecrawl_without_endpoint_blocks_readiness(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_REQUIRE_FIRECRAWL", "0")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["firecrawl_web_search"]["required"] is True
    assert report["firecrawl_web_search"]["mandatory"] is True
    assert report["firecrawl_web_search"]["status"] == "missing_endpoint"
    assert report["checks"]["firecrawl_web_search_ready"] is False
    assert "required Firecrawl web search endpoint is not configured" in " ".join(
        report["warnings"]
    )


def test_required_firecrawl_endpoint_blocks_when_unreachable(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_REQUIRE_FIRECRAWL", "1")
    monkeypatch.setenv("FIRECRAWL_GATEWAY_URL", "http://hub.example.internal:3002")

    def fake_fetch(endpoint, api_key, timeout_seconds):
        raise OSError("connection refused")

    monkeypatch.setattr("mac.hermes_startup._fetch_firecrawl_health", fake_fetch)

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["firecrawl_web_search"]["status"] == "unreachable"
    assert report["checks"]["firecrawl_web_search_ready"] is False
    assert "Firecrawl web search health endpoint is unreachable" in " ".join(report["warnings"])


def test_required_firecrawl_endpoint_ready(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _configure_mandatory_shared_services(monkeypatch, hermes_home)
    monkeypatch.setenv("MAC_REQUIRE_FIRECRAWL", "1")
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://secret@hub.example.internal:3002?token=hidden")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "secret-firecrawl-key")

    def fake_fetch(endpoint, api_key, timeout_seconds):
        assert endpoint == "http://secret@hub.example.internal:3002?token=hidden"
        assert api_key == "secret-firecrawl-key"
        assert timeout_seconds == 2
        return {"ok": True}

    monkeypatch.setattr("mac.hermes_startup._fetch_firecrawl_health", fake_fetch)

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["firecrawl_web_search"]["status"] == "ready"
    assert report["firecrawl_web_search"]["mandatory"] is True
    assert report["firecrawl_web_search"]["endpoint"] == "http://redacted@hub.example.internal:3002"
    assert report["checks"]["firecrawl_web_search_ready"] is True
    rendered = str(report)
    assert "secret-firecrawl-key" not in rendered
    assert "token=hidden" not in rendered


def test_required_task_project_runtime_context_reports_mac_authority(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    mac_home = tmp_path / ".mac"
    workspace = tmp_path / "workspace" / "mac"
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        workspace / ".mac" / "project.yaml",
        "\n".join(
            [
                "schema: mac.repository_contract.v1",
                "project: repo-beads-mac",
                "toolchain:",
                "  required_commands:",
                "    - python3",
                "    - git",
                "test:",
                "  command: scripts/run-contract-tests.sh",
                "",
            ]
        ),
    )
    _prepare_direct_session_tools(monkeypatch, mac_home, workspace)
    _configure_mandatory_shared_services(monkeypatch, hermes_home)
    context = build_runtime_context(
        agent_name="rocky",
        fleet_name="classic",
        mac_url="http://secret@hub.example.internal:8789?token=hidden",
        hermes_home=hermes_home,
        mac_home=mac_home,
        hermes_instance_id="hermes_rocky",
        agent_id="agent_rocky",
        workspace_path=workspace,
    )
    _write(context_path, json.dumps(context))
    _write(markdown_path, render_runtime_markdown(context))
    _write(
        agent_dir / "agent" / "prompt_builder.py",
        "_load_mac_runtime_context\nMAC_HERMES_RUNTIME_CONTEXT_MARKDOWN\nmac-runtime-context.md\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is True
    assert report["task_project_runtime"]["status"] == "ready"
    assert report["task_project_runtime"]["schema"] == "mac.hermes.runtime_context.v1"
    assert report["task_project_runtime"]["authority"]["tasks"] == "mac"
    assert report["task_project_runtime"]["authority"]["projects"] == "mac"
    assert report["task_project_runtime"]["authority"]["agents"] == "mac"
    assert report["task_project_runtime"]["authority"]["fleets"] == "mac"
    assert report["task_project_runtime"]["hermes_instance_id"] == "hermes_rocky"
    assert report["task_project_runtime"]["agent_id"] == "agent_rocky"
    assert report["task_project_runtime"]["mac_url"] == "http://hub.example.internal:8789"
    assert report["task_project_runtime"]["workspace"]["path"] == str(workspace)
    assert (
        report["task_project_runtime"]["workspace"]["project_contract"]["project"]
        == "repo-beads-mac"
    )
    assert set(report["task_project_runtime"]["first_class_object_names"]) == {
        "fleets",
        "tasks",
        "projects",
        "agents",
    }
    assert report["task_project_runtime"]["first_class_objects"]["fleets"]["authority"] == "mac"
    assert report["task_project_runtime"]["first_class_objects"]["tasks"]["authority"] == "mac"
    assert report["task_project_runtime"]["first_class_objects"]["projects"]["authority"] == "mac"
    assert report["task_project_runtime"]["first_class_objects"]["agents"]["authority"] == "mac"
    # `hgmac` is gone; the startup report no longer carries an hgmac_cli surface.
    assert "hgmac_cli" not in report["task_project_runtime"]["first_class_objects"]["agents"]
    assert "hgmac_cli" not in report["task_project_runtime"]["first_class_objects"]["fleets"]
    assert (
        "/ui?view=fleets&selected={fleet_id}"
        in report["task_project_runtime"]["first_class_objects"]["fleets"]["dashboard_urls"]
    )
    assert (
        "/ui?view=work&selected={task_id}"
        in report["task_project_runtime"]["first_class_objects"]["tasks"]["dashboard_urls"]
    )
    assert (
        "/ui?view=projects&project={project}"
        in report["task_project_runtime"]["first_class_objects"]["projects"]["dashboard_urls"]
    )
    assert (
        "/ui?view=work&project={project}"
        in report["task_project_runtime"]["first_class_objects"]["projects"]["dashboard_urls"]
    )
    assert (
        "/ui?view=agents&selected={agent_id}"
        in report["task_project_runtime"]["first_class_objects"]["agents"]["dashboard_urls"]
    )
    assert report["task_project_runtime"]["markdown_contract"]["ready"] is True
    assert report["task_project_runtime"]["markdown_contract"]["missing_snippets"] == []
    assert "shell_execution" in report["task_project_runtime"]["session_capability_names"]
    assert "workspace_file_access" in report["task_project_runtime"]["session_capability_names"]
    assert "ticket_mirror" in report["task_project_runtime"]["session_capability_names"]
    assert "mac_task_cli" in report["task_project_runtime"]["session_capability_names"]
    assert "hermes_oneshot_executor" in report["task_project_runtime"]["session_capability_names"]
    assert "command_audit" in report["task_project_runtime"]["session_capability_names"]
    availability = report["task_project_runtime"]["session_capability_availability"]
    assert availability["ready"] is True
    assert availability["missing"] == []
    rows_by_name = {item["name"]: item for item in availability["capabilities"]}
    assert rows_by_name["shell_execution"]["checks"]["shell_probe_succeeded"] is True
    assert rows_by_name["workspace_file_access"]["checks"]["workspace_write_probe"] is True
    assert {item["name"] for item in availability["capabilities"] if item["ready"]} >= {
        "mac_cli",
        "mac_hermes_cli",
        "shell_execution",
        "workspace_file_access",
        "ticket_mirror",
        "mac_task_cli",
        "quality_gate",
        "hermes_oneshot_executor",
        "command_audit",
        "web_search",
    }
    assert report["checks"]["task_project_runtime_context_available"] is True
    assert report["checks"]["task_project_runtime_markdown_contract_present"] is True
    assert report["checks"]["task_project_runtime_prompt_bridge_active"] is True
    assert report["checks"]["mac_task_project_authority_declared"] is True
    assert report["checks"]["mac_first_class_object_model_declared"] is True
    assert report["checks"]["mac_session_capability_contract_declared"] is True
    assert report["checks"]["mac_session_capabilities_available"] is True
    assert report["task_project_runtime"]["prompt_bridge"]["present"] is True
    rendered = str(report)
    assert "token=hidden" not in rendered
    assert "private runtime command notes" not in rendered


def test_required_task_project_runtime_context_blocks_when_session_tools_missing(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    workspace = tmp_path / "workspace" / "mac"
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        workspace / ".mac" / "project.yaml",
        "\n".join(
            [
                "schema: mac.repository_contract.v1",
                "project: repo-beads-mac",
                "toolchain:",
                "  required_commands:",
                "    - python3",
                "    - git",
                "test:",
                "  command: scripts/run-contract-tests.sh",
                "",
            ]
        ),
    )
    context = build_runtime_context(
        agent_name="rocky",
        fleet_name="classic",
        mac_url="http://hub.example.internal:8789",
        hermes_home=hermes_home,
        mac_home=tmp_path / ".mac",
        hermes_instance_id="hermes_rocky",
        agent_id="agent_rocky",
        workspace_path=workspace,
    )
    _write(context_path, json.dumps(context))
    _write(markdown_path, render_runtime_markdown(context))
    _write(
        agent_dir / "agent" / "prompt_builder.py",
        "_load_mac_runtime_context\nMAC_HERMES_RUNTIME_CONTEXT_MARKDOWN\nmac-runtime-context.md\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["task_project_runtime"]["status"] == "session_capability_unavailable"
    assert report["checks"]["mac_session_capabilities_available"] is False
    missing = report["task_project_runtime"]["session_capability_availability"]["missing"]
    assert "mac_cli" in missing
    assert "quality_gate" in missing


def test_required_task_project_runtime_context_blocks_when_markdown_contract_missing(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    mac_home = tmp_path / ".mac"
    workspace = tmp_path / "workspace" / "mac"
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        workspace / ".mac" / "project.yaml",
        "\n".join(
            [
                "schema: mac.repository_contract.v1",
                "project: repo-beads-mac",
                "toolchain:",
                "  required_commands:",
                "    - python3",
                "    - git",
                "test:",
                "  command: scripts/run-contract-tests.sh",
                "",
            ]
        ),
    )
    _prepare_direct_session_tools(monkeypatch, mac_home, workspace)
    context = build_runtime_context(
        agent_name="rocky",
        fleet_name="classic",
        mac_url="http://hub.example.internal:8789",
        hermes_home=hermes_home,
        mac_home=mac_home,
        hermes_instance_id="hermes_rocky",
        agent_id="agent_rocky",
        workspace_path=workspace,
    )
    _write(context_path, json.dumps(context))
    _write(markdown_path, "MAC Task and Project Runtime\n")
    _write(
        agent_dir / "agent" / "prompt_builder.py",
        "_load_mac_runtime_context\nMAC_HERMES_RUNTIME_CONTEXT_MARKDOWN\nmac-runtime-context.md\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    runtime = report["task_project_runtime"]
    assert runtime["status"] == "markdown_contract_missing"
    assert runtime["markdown_contract"]["ready"] is False
    assert "First-Class Objects" in runtime["markdown_contract"]["missing_snippets"]
    assert "mac-hermes tasks" in runtime["markdown_contract"]["missing_snippets"]
    assert report["checks"]["task_project_runtime_markdown_contract_present"] is False


def test_required_task_project_runtime_context_blocks_readiness_when_missing(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["task_project_runtime"]["status"] == "missing_context"
    assert report["checks"]["task_project_runtime_context_available"] is False
    assert "runtime context file is missing" in " ".join(report["warnings"])


def test_required_task_project_runtime_context_blocks_when_object_model_missing(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    context = build_runtime_context(
        agent_name="rocky",
        fleet_name="classic",
        mac_url="http://hub.example.internal:8789",
        hermes_home=hermes_home,
        mac_home=tmp_path / ".mac",
        hermes_instance_id="hermes_rocky",
        agent_id="agent_rocky",
    )
    context.pop("first_class_objects")
    _write(context_path, json.dumps(context))
    _write(markdown_path, "runtime")
    _write(
        agent_dir / "agent" / "prompt_builder.py",
        "_load_mac_runtime_context\nMAC_HERMES_RUNTIME_CONTEXT_MARKDOWN\nmac-runtime-context.md\n",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["task_project_runtime"]["status"] == "first_class_object_contract_missing"
    assert report["checks"]["mac_first_class_object_model_declared"] is False
    assert "first-class object contract" in " ".join(report["warnings"])


def test_required_task_project_runtime_context_blocks_when_prompt_bridge_missing(
    monkeypatch,
    tmp_path,
):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    agent_dir = tmp_path / "hermes-agent"
    context_path = hermes_home / "mac-runtime-context.json"
    markdown_path = hermes_home / "mac-runtime-context.md"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        context_path,
        json.dumps(
            build_runtime_context(
                agent_name="rocky",
                fleet_name="classic",
                mac_url="http://hub.example.internal:8789",
                hermes_home=hermes_home,
                mac_home=tmp_path / ".mac",
                hermes_instance_id="hermes_rocky",
                agent_id="agent_rocky",
            )
        ),
    )
    _write(markdown_path, "runtime")
    # The vendored Hermes tree was removed on 2026-08-17, so MAC_HERMES_AGENT_DIR
    # is now the only way to point the prompt-bridge check at a runtime. Aim it
    # at a tree whose prompt_builder LACKS the mac-runtime-context bridge to
    # exercise the "missing" path.
    _write(agent_dir / "agent" / "prompt_builder.py", "def build_context_files_prompt(): pass\n")
    monkeypatch.setenv("MAC_HERMES_AGENT_DIR", str(agent_dir))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["task_project_runtime"]["prompt_bridge"]["present"] is False
    assert report["checks"]["task_project_runtime_prompt_bridge_active"] is False
    assert "runtime prompt bridge is missing" in " ".join(report["warnings"])


def test_qdrant_degraded_override_does_not_allow_startup(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_REQUIRE_QDRANT_MEMORY", "1")
    monkeypatch.setenv("MAC_QDRANT_MEMORY_ALLOW_DEGRADED", "1")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["qdrant_level2"]["status"] == "missing_endpoint"
    assert report["qdrant_level2"]["degraded_allowed"] is False
    assert report["qdrant_level2"]["degradation_reason"]
    assert "required Qdrant shared memory endpoint is not configured" in " ".join(
        report["warnings"]
    )


def test_qdrant_disable_flag_does_not_allow_startup(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _configure_mandatory_shared_services(monkeypatch, hermes_home)
    monkeypatch.setenv("MAC_QDRANT_MEMORY", "0")

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["qdrant_level2"]["status"] == "disabled_by_env"
    assert report["qdrant_level2"]["mandatory"] is True
    assert "Qdrant shared memory is mandatory and cannot be disabled" in " ".join(
        report["warnings"]
    )


def test_startup_includes_gateway_log_classification(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    hermes_home = tmp_path / ".hermes"
    log_summary = tmp_path / "hermes-log-summary.json"
    _write(hermes_home / "config.yaml", "model: local\n")
    _write(hermes_home / "SOUL.md", "soul")
    _write(hermes_home / "MEMORY.md", "memory")
    _write(hermes_home / "state.db", "state")
    _write(
        log_summary,
        '{"classes":[{"name":"secret_redaction_disabled","severity":"critical","count":1}],"actionable_count":1,"benign_count":0}',
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("MAC_HERMES_LOG_SUMMARY", str(log_summary))

    report = build_hermes_startup_report()

    assert report["ready"] is False
    assert report["logs"]["actionable_count"] == 1
    assert "discord_missing_token_unconfigured" in report["logs"]["known_benign_classes"]
    assert report["operator_health"]["status"] == "degraded"


def test_api_exposes_hermes_startup_report_and_can_fail_closed(monkeypatch, tmp_path):
    _clear_startup_env(monkeypatch)
    missing_home = tmp_path / "missing-hermes"
    monkeypatch.setenv("HERMES_HOME", str(missing_home))
    monkeypatch.setenv("ACC_DIR", str(tmp_path / ".acc"))

    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))
    response = client.get("/startup/hermes")

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["ready"] is False
    assert "Hermes home does not exist" in " ".join(payload["warnings"])

    monkeypatch.setenv("MAC_REQUIRE_HERMES_STARTUP_READY", "1")
    with pytest.raises(ValidationError):
        create_app(control_plane=ControlPlane.in_memory())


# --- relocated from test_hermes_startup_edges.py (coverage companion folded in) ---


def _write_edges(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _runtime_paths(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    home.mkdir()
    context_path = home / "context.json"
    markdown_path = home / "context.md"
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_FILE", str(context_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN", str(markdown_path))
    monkeypatch.setenv("MAC_HERMES_RUNTIME_CONTEXT_REQUIRED", "1")
    return (home, context_path, markdown_path)


def test_file_url_command_and_topology_helpers_cover_error_paths(tmp_path, monkeypatch):

    class BrokenPath:
        def __str__(self):
            return "/broken"

        def exists(self):
            raise OSError("broken stat")

    assert startup._file_ref(BrokenPath(), "broken", False)["exists"] is False
    directory = tmp_path / "dir"
    directory.mkdir()
    ref = startup._file_ref(directory, "dir", False)
    assert ref["kind"] == "dir"
    assert startup._read_small_text(directory) == ""
    text = tmp_path / "text"
    text.write_bytes(b"abcdef")
    assert startup._read_small_text(text, limit=3) == "abc"
    assert startup._redact_url("not-a-url") == "<invalid-url>"
    assert (
        startup._redact_url("http://user:pass@[::1]:8789/path/?secret=1")
        == "http://redacted@[::1]:8789/path"
    )
    assert startup._command_name("'unterminated") == "'unterminated"
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(493)
    assert startup._command_resolution(str(executable))["available"] is True
    local = tmp_path / "bin" / "tool"
    local.parent.mkdir()
    local.write_text("#!/bin/sh\n", encoding="utf-8")
    local.chmod(493)
    assert startup._command_resolution("bin/tool", cwd=str(tmp_path))["local_candidate"] == str(
        local
    )
    assert startup._command_resolution("missing", expected_path=str(executable))["resolved"] == str(
        executable
    )
    topology = tmp_path / "topology.json"
    topology.write_text("not-json", encoding="utf-8")
    assert startup._topology_summary(topology)["error"]
    topology.write_text("[]", encoding="utf-8")
    assert startup._topology_summary(topology)["error"] == "memory topology root is not an object"
    topology.write_text(
        json.dumps(
            {
                "schema": "topology",
                "agent": "agent",
                "hub": {"agent": "hub", "url": "http://user:pass@hub:8789?q=secret"},
                "shared_services": {"qdrant": {"url": "http://qdrant:6333"}},
            }
        ),
        encoding="utf-8",
    )
    summary = startup._topology_summary(topology)
    assert summary["hub_url"] == "http://redacted@hub:8789"
    assert summary["qdrant_url"] == "http://qdrant:6333"


@pytest.mark.parametrize(
    "payload,expected", [("not-json", "invalid_context"), ("[]", "invalid_context")]
)
def test_runtime_context_rejects_invalid_content(tmp_path, monkeypatch, payload, expected):
    home, context_path, _markdown_path = _runtime_paths(tmp_path, monkeypatch)
    context_path.write_text(payload, encoding="utf-8")
    report = startup._runtime_context_summary(home)
    assert report["status"] == expected
    assert report["ready"] is False
    assert report["warning"]


def test_runtime_context_contract_failure_matrix(tmp_path, monkeypatch):
    home, context_path, markdown_path = _runtime_paths(tmp_path, monkeypatch)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    context = build_runtime_context(
        agent_name="agent",
        fleet_name="fleet",
        mac_url="http://hub:8789",
        hermes_home=home,
        mac_home=tmp_path / "mac",
        hermes_instance_id="hermes_agent",
        agent_id="agent_agent",
        workspace_path=workspace,
    )
    markdown_path.write_text(render_runtime_markdown(context), encoding="utf-8")
    cases = []
    invalid_schema = json.loads(json.dumps(context))
    invalid_schema["schema"] = "wrong"
    cases.append((invalid_schema, "invalid_schema"))
    authority = json.loads(json.dumps(context))
    authority["authority"]["tasks"] = "other"
    cases.append((authority, "authority_mismatch"))
    missing_capability = json.loads(json.dumps(context))
    missing_capability["session_capabilities"]["capabilities"] = [
        item
        for item in missing_capability["session_capabilities"]["capabilities"]
        if item.get("name") != "web_search"
    ]
    cases.append((missing_capability, "session_capability_contract_missing"))
    for value, expected in cases:
        context_path.write_text(json.dumps(value), encoding="utf-8")
        report = startup._runtime_context_summary(home)
        assert report["status"] == expected
        assert report["ready"] is False
    context_path.write_text(json.dumps(context), encoding="utf-8")
    markdown_path.unlink()
    assert startup._runtime_context_summary(home)["status"] == "missing_markdown"


def test_session_capability_probes_record_failures(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    contract = workspace / "project.yaml"
    contract.write_text("schema: test", encoding="utf-8")
    monkeypatch.setattr(startup.shutil, "which", lambda _command: None)
    monkeypatch.setattr(
        startup.subprocess,
        "run",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no shell")),
    )
    monkeypatch.setattr(startup.os, "access", lambda *_args: False)
    capabilities = [
        {"name": "shell_execution", "required": True, "command": "sh"},
        {"name": "workspace_file_access", "required": True, "cwd": str(workspace)},
        {"name": "mac_api", "required": True, "endpoint": "<invalid-url>"},
        {"name": "web_search", "required": True, "environment": ["MISSING_SEARCH"]},
    ]
    report = startup._session_capability_availability(
        capabilities,
        workspace={
            "path": str(workspace),
            "project_contract": {
                "path": str(contract),
                "schema": "wrong",
                "required_commands": ["missing-command"],
            },
        },
    )
    assert report["ready"] is False
    assert "project_contract" in report["missing"]
    assert "project_toolchain:missing-command" in report["missing"]
    assert {"shell_execution", "workspace_file_access", "mac_api", "web_search"} <= set(
        report["missing"]
    )


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


def test_shared_service_fetch_helpers_build_authenticated_requests(monkeypatch):
    seen = []

    def open_url(request, timeout):
        seen.append((request, timeout))
        return _Response(b'{"ok": true}')

    monkeypatch.setattr(startup.urllib.request, "urlopen", open_url)
    assert startup._fetch_qdrant_collections("http://qdrant/", "key", 3) == {"ok": True}
    assert seen[-1][0].headers["Api-key"] == "key"
    assert startup._fetch_firecrawl_health("http://search/", "token", 4) == {"ok": True}
    assert seen[-1][0].headers["Authorization"] == "Bearer token"
    assert startup._fetch_tokenhub_json("http://tokenhub/", "/healthz", "token", 5) == {"ok": True}
    assert seen[-1][0].headers["Authorization"] == "Bearer token"
    monkeypatch.setattr(startup.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(b""))
    assert startup._fetch_qdrant_collections("http://qdrant", None, 1) == {}
    assert startup._fetch_firecrawl_health("http://search", "none", 1) == {}
    assert startup._fetch_tokenhub_json("http://tokenhub", "/healthz", None, 1) == {}


def test_qdrant_and_firecrawl_report_failure_matrix(tmp_path, monkeypatch):
    topology = tmp_path / "topology.json"
    monkeypatch.setenv("MAC_MEMORY_TOPOLOGY_FILE", str(topology))
    monkeypatch.setenv("QDRANT_URL", "not-a-url")
    assert startup._qdrant_memory_report(tmp_path)["status"] == "invalid_endpoint"
    monkeypatch.setenv("QDRANT_URL", "http://qdrant:6333")
    assert startup._qdrant_memory_report(tmp_path)["status"] == "missing_topology"
    topology.write_text("not-json", encoding="utf-8")
    assert startup._qdrant_memory_report(tmp_path)["status"] == "invalid_topology"
    topology.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        startup,
        "_fetch_qdrant_collections",
        lambda *_args: (_ for _ in ()).throw(OSError("offline")),
    )
    assert startup._qdrant_memory_report(tmp_path)["status"] == "unreachable"
    monkeypatch.setenv("FIRECRAWL_API_URL", "invalid")
    assert startup._firecrawl_web_search_report()["status"] == "invalid_endpoint"


def test_tokenhub_report_required_degraded_and_transport_matrix(monkeypatch):
    for name in (
        "TOKENHUB_URL",
        "MAC_TOKENHUB_URL",
        "MAC_REQUIRE_TOKENHUB",
        "MAC_TOKENHUB_ALLOW_DEGRADED",
        "TOKENHUB_API_KEY",
        "TOKENHUB_AGENT_KEY",
        "OPENAI_API_KEY",
        "MAC_ROUTER_BACKEND",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAC_ROUTER_BACKEND", "inproc")
    assert startup._tokenhub_report()["status"] == "retired"
    monkeypatch.delenv("MAC_ROUTER_BACKEND")
    monkeypatch.setenv("MAC_REQUIRE_TOKENHUB", "1")
    assert startup._tokenhub_report()["status"] == "missing_endpoint"
    monkeypatch.setenv("MAC_TOKENHUB_ALLOW_DEGRADED", "1")
    assert startup._tokenhub_report()["status"] == "degraded_allowed"
    monkeypatch.setenv("TOKENHUB_URL", "invalid")
    assert startup._tokenhub_report()["status"] == "degraded_allowed"
    monkeypatch.setenv("MAC_TOKENHUB_ALLOW_DEGRADED", "0")
    assert startup._tokenhub_report()["status"] == "invalid_endpoint"
    monkeypatch.setenv("TOKENHUB_URL", "http://tokenhub:8090")
    monkeypatch.setattr(
        startup, "_fetch_tokenhub_json", lambda *_args: (_ for _ in ()).throw(OSError("offline"))
    )
    assert startup._tokenhub_report()["status"] == "unreachable"
    monkeypatch.setattr(startup, "_fetch_tokenhub_json", lambda *_args: {"adapters": 2})
    assert startup._tokenhub_report()["status"] == "missing_client_key"
    monkeypatch.setenv("TOKENHUB_API_KEY", "token")

    def models_fail(_endpoint, path, _key, _timeout):
        if path == "/healthz":
            return {"adapters": 2}
        raise OSError("models offline")

    monkeypatch.setattr(startup, "_fetch_tokenhub_json", models_fail)
    assert startup._tokenhub_report()["status"] == "models_unreachable"


def test_redaction_slack_and_log_helpers_cover_fallbacks(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text("SLACK_BOT_TOKEN: xoxb\n", encoding="utf-8")
    assert startup._config_explicitly_enables_slack(config)
    config.write_text("slack:\n  enabled: true\nnext: value\n", encoding="utf-8")
    assert startup._config_explicitly_enables_slack(config)
    config.write_text("slack: {enabled: true}\n", encoding="utf-8")
    assert startup._config_explicitly_enables_slack(config)
    assert startup._bool_value("maybe") is None
    env_file = tmp_path / ".env"
    env_file.write_text("# comment\nBROKEN\nHERMES_REDACT_SECRETS=maybe\n", encoding="utf-8")
    assert startup._env_file_redaction_ref(env_file, "env")["redact_secrets"] == "invalid"
    config.write_text("redact_secrets: no\n", encoding="utf-8")
    assert startup._config_redaction_value(config) == "false"
    config.write_text("redact_secrets: maybe\n", encoding="utf-8")
    assert startup._config_redaction_value(config) == "unset"
    summary = tmp_path / "summary.json"
    summary.write_text("not-json", encoding="utf-8")
    monkeypatch.setenv("MAC_HERMES_LOG_SUMMARY", str(summary))
    assert startup._log_classification_report()["warnings"]
