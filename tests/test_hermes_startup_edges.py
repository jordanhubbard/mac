"""Failure-matrix coverage for Hermes startup diagnostics."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mac import hermes_startup as startup
from mac.hermes_runtime import build_runtime_context, render_runtime_markdown


def _write(path: Path, content: str) -> None:
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
    return home, context_path, markdown_path


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
    assert startup._redact_url("http://user:pass@[::1]:8789/path/?secret=1") == "http://redacted@[::1]:8789/path"
    assert startup._command_name("'unterminated") == "'unterminated"
    executable = tmp_path / "tool"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    assert startup._command_resolution(str(executable))["available"] is True
    local = tmp_path / "bin" / "tool"
    local.parent.mkdir()
    local.write_text("#!/bin/sh\n", encoding="utf-8")
    local.chmod(0o755)
    assert startup._command_resolution("bin/tool", cwd=str(tmp_path))["local_candidate"] == str(local)
    assert startup._command_resolution("missing", expected_path=str(executable))["resolved"] == str(executable)

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
    "payload,expected",
    [
        ("not-json", "invalid_context"),
        ("[]", "invalid_context"),
    ],
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
    assert startup._fetch_tokenhub_json("http://tokenhub/", "/healthz", "token", 5) == {
        "ok": True
    }
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
        startup,
        "_fetch_tokenhub_json",
        lambda *_args: (_ for _ in ()).throw(OSError("offline")),
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
