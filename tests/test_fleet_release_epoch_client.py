from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "fleet-release-epoch-client.py"
AUTHORITY_ID = "123e4567-e89b-12d3-a456-426614174000"
DIGEST = "sha256:" + ("a" * 64)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fleet_release_epoch_client", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _private(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")
    path.chmod(0o600)


def _receipt(status: str = "open") -> dict[str, Any]:
    value: dict[str, Any] = {
        "schema": "mac.fleet_release_epoch_receipt.v1",
        "status": status,
        "epoch_id": "epoch-one",
        "hub_authority_id": AUTHORITY_ID,
        "identity_sha256": DIGEST,
        "cohort_size": 0,
        "successor_hold_reason": None,
        "desired_worker_credential_mode": "compatibility",
        "prepared_at": "2100-01-01T00:00:00+00:00",
        "agents": [],
    }
    if status in {"proved", "committed"}:
        value["proof_sha256"] = "sha256:" + ("b" * 64)
        value["proved_at"] = "2100-01-01T00:00:01+00:00"
    if status == "committed":
        value["committed_at"] = "2100-01-01T00:00:02+00:00"
    return value


def test_loopback_url_and_private_file_contract(tmp_path: Path) -> None:
    client = _module()
    assert client._hub_url("http://127.0.0.1:8789/") == "http://127.0.0.1:8789"
    for unsafe in (
        "https://127.0.0.1:8789",
        "http://hub.example:8789",
        "http://user@127.0.0.1:8789",
        "http://127.0.0.1:8789/api",
    ):
        with pytest.raises(client.ClientError):
            client._hub_url(unsafe)

    token = tmp_path / "token"
    _private(token, "secret-token\n")
    assert client._token(token) == "secret-token"
    token.chmod(0o644)
    with pytest.raises(client.ClientError):
        client._token(token)


def test_authority_and_receipts_are_exactly_bound() -> None:
    client = _module()
    assert client._authority(
        {
            "schema": "mac.fleet_release_hub_authority.v1",
            "hub_authority_id": AUTHORITY_ID.upper(),
        }
    )["hub_authority_id"] == AUTHORITY_ID
    with pytest.raises(client.ClientError):
        client._authority(
            {
                "schema": "mac.fleet_release_hub_authority.v1",
                "hub_authority_id": AUTHORITY_ID,
                "token": "leak",
            }
        )

    assert client._participant_state(
        {
            "id": "agent_one",
            "last_seen_at": "2100-01-01T00:00:00+00:00",
            "dispatch_hold": True,
            "dispatch_hold_reason": "operator hold",
            "dispatch_hold_at": "2100-01-01T00:00:00+00:00",
            "resources": {"token": "must not be copied"},
        },
        "agent_one",
    ) == {
        "schema": "mac.fleet_release_participant_state.v1",
        "agent_id": "agent_one",
        "baseline_seen": "2100-01-01T00:00:00+00:00",
        "expected_dispatch_hold": True,
        "expected_hold_reason": "operator hold",
        "expected_hold_at": "2100-01-01T00:00:00+00:00",
    }

    assert client._receipt(
        _receipt("committed"),
        expected_epoch="epoch-one",
        expected_status="committed",
        expected_identity=DIGEST,
    )["status"] == "committed"
    changed = _receipt("committed")
    changed["identity_sha256"] = "sha256:" + ("c" * 64)
    with pytest.raises(client.ClientError):
        client._receipt(
            changed,
            expected_epoch="epoch-one",
            expected_status="committed",
            expected_identity=DIGEST,
        )


def test_main_writes_private_receipt_without_printing_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _module()
    token = tmp_path / "token"
    request = tmp_path / "request.json"
    output = tmp_path / "receipt.json"
    _private(token, "admin-token\n")
    _private(request, json.dumps({"epoch_id": "epoch-one", "participants": []}))
    monkeypatch.setattr(client, "_request", lambda *args, **kwargs: _receipt("open"))
    assert (
        client.main(
            [
                "--token-file",
                str(token),
                "open",
                "--epoch",
                "epoch-one",
                "--request-file",
                str(request),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["identity_sha256"] == DIGEST
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    stdout = capsys.readouterr().out
    assert "admin-token" not in stdout
    assert DIGEST not in stdout


def test_redirect_handler_fails_closed() -> None:
    client = _module()
    with pytest.raises(client.ClientError):
        client._NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://elsewhere")


def test_http_error_detail_allows_only_plain_fleet_release_validation() -> None:
    client = _module()
    assert (
        client._safe_http_error_detail(
            json.dumps(
                {
                    "detail": (
                        "fleet release epoch lost node readiness for agent_rocky"
                    )
                }
            ).encode()
        )
        == "fleet release epoch lost node readiness for agent_rocky"
    )
    for unsafe in (
        b'{"detail":"token=secret"}',
        b'{"detail":"fleet release leaked /private/path"}',
        b'{"detail":{"message":"fleet release nested payload"}}',
        b'{"detail":"fleet release ok","request":{"token":"secret"}}',
        b"not-json",
    ):
        assert client._safe_http_error_detail(unsafe) == ""


def test_helper_is_executable() -> None:
    assert os.access(HELPER, os.X_OK)
