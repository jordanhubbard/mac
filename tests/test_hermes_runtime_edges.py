from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from mac import hermes_runtime as runtime


def test_connection_url_invalid_and_ipv6_redaction() -> None:
    assert runtime.connection_url(" local-address ") == "local-address"
    assert runtime.connection_url("https://user:secret@[::1]:8443/path/?token=x") == (
        "https://[::1]:8443/path"
    )


def test_set_env_preserves_comments_replaces_removes_and_appends(tmp_path: Path) -> None:
    path = tmp_path / "config" / ".env"
    path.parent.mkdir()
    path.write_text(
        "# heading\n\nMALFORMED\nKEEP=old\nREPLACE=old\nREMOVE=old\n",
        encoding="utf-8",
    )
    runtime.set_env(
        path,
        {"REPLACE": "new", "REMOVE": None, "ADDED": "yes", "SKIP": None},
    )
    text = path.read_text(encoding="utf-8")
    assert "# heading" in text
    assert "MALFORMED" in text
    assert "KEEP=old" in text
    assert "REPLACE=new" in text
    assert "REMOVE=" not in text
    assert text.endswith("ADDED=yes\n")
    assert path.stat().st_mode & 0o777 == 0o600


def test_repository_contract_invalid_yaml_and_non_object(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    contract = workspace / ".mac" / "project.yaml"
    contract.parent.mkdir(parents=True)
    contract.write_text("root: [unterminated", encoding="utf-8")
    assert "error" in runtime._repository_contract(workspace)
    contract.write_text("- a\n- b\n", encoding="utf-8")
    assert runtime._repository_contract(workspace)["error"] == (
        "repository contract root is not an object"
    )


def test_main_writes_context_and_reports_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, Any] = {}

    def fake_write_runtime_context(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {
            "agent": {"agent_id": "agent_edge"},
            "identity": {"hermes_instance_id": "hermes_edge"},
            "endpoints": {"mac_api": ""},
        }

    monkeypatch.setattr(runtime, "write_runtime_context", fake_write_runtime_context)
    result = runtime._main(
        [
            str(tmp_path / "context.json"),
            str(tmp_path / "context.md"),
            str(tmp_path / ".env"),
            "--agent-name",
            "Edge",
            "--fleet-name",
            "fleet",
            "--mac-url",
            "http://mac",
            "--hermes-home",
            str(tmp_path / "hermes"),
            "--mac-home",
            str(tmp_path / "mac"),
            "--tenant-id",
            "tenant",
            "--persona-id",
            "persona",
            "--hermes-instance-id",
            "hermes",
            "--agent-id",
            "agent",
            "--workspace",
            str(tmp_path / "workspace"),
        ]
    )
    assert result == 0
    assert captured["agent_name"] == "Edge"
    assert captured["workspace_path"] == tmp_path / "workspace"
    assert "mac_url=unconfigured" in capsys.readouterr().out
