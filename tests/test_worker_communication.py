from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from mac.worker import MacWorker, WorkerExecution


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []

    def get(self, path: str):
        self.calls.append(("GET", path, None))
        if path.startswith("/communication/accounts?"):
            return [
                {
                    "id": "commacct_1",
                    "identity_id": "commid_hive",
                    "channel": "slack",
                    "account_id": "operations",
                    "enabled": True,
                }
            ]
        if path == "/communication/accounts/commacct_1":
            return {
                "id": "commacct_1",
                "channel": "slack",
                "account_id": "operations",
            }
        return {}

    def post(self, path: str, body):
        self.calls.append(("POST", path, body))
        if path == "/communication/gateway-leases/acquire":
            return {"id": "gwlease_1"}
        if path == "/communication/deliveries/claim":
            return [
                {
                    "id": "delivery_1",
                    "account_id": "commacct_1",
                    "channel": "slack",
                    "target": "channel:C123",
                    "body": "Build complete",
                }
            ]
        if path.endswith("/ack"):
            return {"status": "delivered"}
        return {}


def _worker(tmp_path: Path, client: _Client) -> MacWorker:
    return MacWorker(
        client,
        "agent_gateway",
        tmp_path,
        lambda _task, _task_dir: WorkerExecution(0, "unused"),
    )


def test_gateway_worker_acquires_identity_lease_and_sends_only_through_openclaw(
    monkeypatch, tmp_path: Path
) -> None:
    message_bin = tmp_path / "openclaw-message"
    message_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    message_bin.chmod(0o700)
    monkeypatch.setenv("MAC_OPENCLAW_PUBLIC_IDENTITY", "mac-hive")
    monkeypatch.setenv("MAC_OPENCLAW_MESSAGE_BIN", str(message_bin))
    client = _Client()
    worker = _worker(tmp_path, client)
    completed_commands: list[list[str]] = []

    def fake_run(command, **_kwargs):
        completed_commands.append(command)
        return SimpleNamespace(returncode=0, stdout='{"messageId":"123.456"}', stderr="")

    monkeypatch.setattr("mac.worker.subprocess.run", fake_run)
    worker._maintain_openclaw_gateway_leases()
    worker._process_human_delivery_outbox()

    assert any(call[1] == "/communication/gateway-leases/acquire" for call in client.calls)
    assert completed_commands == [
        [
            str(message_bin),
            "send",
            "--channel",
            "slack",
            "--account",
            "operations",
            "--target",
            "channel:C123",
            "--message",
            "Build complete",
            "--json",
        ]
    ]
    ack = next(call for call in client.calls if call[1].endswith("/ack"))
    assert ack[2]["provider_message_id"] == "123.456"
    assert ack[2]["detail"]["openclaw"] is True


def test_headless_worker_does_not_claim_human_deliveries(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("MAC_OPENCLAW_PUBLIC_IDENTITY", raising=False)
    client = _Client()
    worker = _worker(tmp_path, client)
    worker._maintain_openclaw_gateway_leases()
    worker._process_human_delivery_outbox()
    assert client.calls == []


def test_openclaw_worker_never_falls_back_to_direct_slack_sdk(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("MAC_CHAT_GATEWAY_IMPL", "openclaw")
    result = _worker(tmp_path, _Client())._send_status_update_to_home_channels(
        {"channel_type": "slack"}
    )
    assert result["status"] == "skipped"
    assert result["reason"] == "openclaw_outbox_required"
