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


def test_delivery_drain_thread_drains_while_task_loop_is_busy(
    monkeypatch, tmp_path: Path
) -> None:
    """task_c049302b: the outbox must drain independently of the task loop.

    A worker stuck in a long task iteration previously starved its gateway's
    outbox (observed live: a delivery sat pending, attempt_count=0, while the
    origin agent was busy). The background drain thread claims and sends even
    when run_once never comes back around."""
    import threading
    import time

    message_bin = tmp_path / "openclaw-message"
    message_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    message_bin.chmod(0o700)
    monkeypatch.setenv("MAC_OPENCLAW_PUBLIC_IDENTITY", "mac-hive")
    monkeypatch.setenv("MAC_OPENCLAW_MESSAGE_BIN", str(message_bin))
    client = _Client()
    worker = _worker(tmp_path, client)
    worker.delivery_drain_interval_seconds = 0.05
    drained = threading.Event()

    def fake_run(command, **_kwargs):
        drained.set()
        return SimpleNamespace(returncode=0, stdout='{"messageId":"9.9"}', stderr="")

    monkeypatch.setattr("mac.worker.subprocess.run", fake_run)
    # Simulate "task loop busy": nothing calls _process_human_delivery_outbox
    # from the loop; only the thread may drain.
    worker._start_delivery_drain_thread()
    try:
        assert drained.wait(timeout=5.0), "drain thread never claimed the delivery"
    finally:
        worker._stop_delivery_drain_thread()
    assert any(call[1] == "/communication/deliveries/claim" for call in client.calls)
    # Stopping is idempotent and actually stops future drains.
    calls_after_stop = len(client.calls)
    time.sleep(0.2)
    assert len(client.calls) == calls_after_stop


def test_delivery_drain_skips_when_another_drain_holds_the_lock(
    monkeypatch, tmp_path: Path
) -> None:
    """Concurrent loop+thread drains must not double-claim from this process:
    the second entrant skips (hub-side leases already prevent double-send
    across processes; the lock avoids redundant claim storms within one)."""
    message_bin = tmp_path / "openclaw-message"
    message_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    message_bin.chmod(0o700)
    monkeypatch.setenv("MAC_OPENCLAW_PUBLIC_IDENTITY", "mac-hive")
    monkeypatch.setenv("MAC_OPENCLAW_MESSAGE_BIN", str(message_bin))
    client = _Client()
    worker = _worker(tmp_path, client)
    with worker._delivery_drain_lock:
        worker._process_human_delivery_outbox()
    assert client.calls == []  # skipped: lock held elsewhere
    monkeypatch.setattr(
        "mac.worker.subprocess.run",
        lambda command, **_kwargs: SimpleNamespace(
            returncode=0, stdout="{}", stderr=""
        ),
    )
    worker._process_human_delivery_outbox()
    assert any(call[1] == "/communication/deliveries/claim" for call in client.calls)
