"""Tests for the ``mac-evidence sign`` CLI.

The interesting invariant is byte-for-byte signature compatibility with
``mac.services.sign_verification_manifest`` — the bash stubs in
``deploy/codex-runner`` and the host worker MUST produce the same HMAC
or evidence written by one path will fail to verify when consumed by
the other.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from typing import Any, Dict

import pytest

from mac.evidence_cli import main as cli_main
from mac.services import sign_verification_manifest


def _sample_manifest() -> Dict[str, Any]:
    # Shape mirrors the operator_result stub produced by
    # deploy/codex-runner/mac-task-executor-codex.
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": "test manifest",
        "result": "executor_completed",
        "executed_at": "2026-01-01T00:00:00Z",
        "checks": [{"name": "executor_returncode", "status": "pass", "returncode": 0}],
    }


def test_sign_produces_byte_identical_signature(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`mac-evidence sign` must produce exactly the same signature as
    a direct call to mac.services.sign_verification_manifest, otherwise
    the bash-stub-vs-host-worker split would break verification."""
    manifest = _sample_manifest()
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    monkeypatch.setenv("MAC_AGENT_ATTESTATION_KEY", "test-key")
    monkeypatch.setenv("MAC_AGENT_ID", "agent-x")

    rc = cli_main(["sign", "--manifest", str(manifest_path)])
    assert rc == 0

    signed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert signed["signed_by"] == "agent-x"

    # Recompute the expected signature using the library directly and
    # compare byte-for-byte.
    expected_input = dict(signed)
    expected_input.pop("signature")
    expected = sign_verification_manifest("test-key", expected_input)
    assert signed["signature"] == expected


def test_sign_missing_key_env_fails_with_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_sample_manifest()), encoding="utf-8")
    monkeypatch.delenv("MAC_AGENT_ATTESTATION_KEY", raising=False)
    monkeypatch.setenv("MAC_AGENT_ID", "agent-y")

    rc = cli_main(["sign", "--manifest", str(manifest_path)])
    assert rc == 2
    err = capsys.readouterr().err
    assert "MAC_AGENT_ATTESTATION_KEY" in err
    # The manifest must not have been silently rewritten.
    loaded = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "signature" not in loaded


def test_sign_manifest_stdin_writes_to_stdout(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAC_AGENT_ATTESTATION_KEY", "stdin-key")
    monkeypatch.setenv("MAC_AGENT_ID", "stdin-signer")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(_sample_manifest())))

    rc = cli_main(["sign", "--manifest-stdin"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert parsed["signed_by"] == "stdin-signer"
    expected_input = dict(parsed)
    expected_input.pop("signature")
    assert parsed["signature"] == sign_verification_manifest("stdin-key", expected_input)


def test_sign_signed_by_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_sample_manifest()), encoding="utf-8")
    monkeypatch.setenv("MAC_AGENT_ATTESTATION_KEY", "k")
    monkeypatch.delenv("MAC_AGENT_ID", raising=False)

    rc = cli_main(
        ["sign", "--manifest", str(manifest_path), "--signed-by", "explicit-agent"]
    )
    assert rc == 0
    signed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert signed["signed_by"] == "explicit-agent"


def test_sign_invalid_json_returns_input_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("not json", encoding="utf-8")
    monkeypatch.setenv("MAC_AGENT_ATTESTATION_KEY", "k")
    monkeypatch.setenv("MAC_AGENT_ID", "agent")
    rc = cli_main(["sign", "--manifest", str(manifest_path)])
    assert rc == 3
    assert "not valid JSON" in capsys.readouterr().err


def test_sign_custom_key_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_sample_manifest()), encoding="utf-8")
    monkeypatch.delenv("MAC_AGENT_ATTESTATION_KEY", raising=False)
    monkeypatch.setenv("CUSTOM_KEY", "alt-key")
    monkeypatch.setenv("MAC_AGENT_ID", "agent-c")
    rc = cli_main(
        ["sign", "--manifest", str(manifest_path), "--key-env", "CUSTOM_KEY"]
    )
    assert rc == 0
    signed = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_input = dict(signed)
    expected_input.pop("signature")
    assert signed["signature"] == sign_verification_manifest("alt-key", expected_input)
