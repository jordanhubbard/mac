from __future__ import annotations

import hashlib
import json
import os
import stat
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from mac.worker_slot import WorkerSlotClient, WorkerSlotConfig, WorkerSlotError

NODE = "1" * 32
INCARNATION = "2" * 32
SESSION = "00000000-0000-4000-8000-000000000001"
TICKET = "a" * 64
RESOURCE = f"node:{NODE}:heavy"
SUBJECT = {
    "node_id": NODE,
    "owner": "fixture-owner",
    "session_id": SESSION,
    "incarnation": INCARNATION,
}


def _future(seconds: int = 120) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def _guide() -> dict:
    return {"contract": "webmux-session-coordination/v1", "subject": SUBJECT}


def _view(**updates) -> dict:
    value = {
        "contract": "webmux-session-coordination/v1",
        "enabled": True,
        "history_epoch": "epoch-1",
        "self": {"ref": SUBJECT, "active": True, "present": True},
        "resources": [
            {
                "id": RESOURCE,
                "authority_node_id": NODE,
                "queue_contract": "webmux-worker-queue/v1",
                "capacity": 1,
            }
        ],
    }
    value.update(updates)
    return value


def _queued(*, ready: bool = False, status: str = "queued", state: str = "waiting") -> dict:
    return {
        "status": status,
        "record_id": TICKET,
        "state": state,
        "queue": {
            "sequence": 42,
            "position": 1,
            "deadline": _future(600),
            "state": state,
            "ready": ready,
        },
    }


def _client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, dict]]):
    fixture = tmp_path / "responses.json"
    fixture.write_text(json.dumps(responses))
    binary = tmp_path / "webmux"
    binary.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, pathlib, sys\n"
        "log = pathlib.Path(os.environ['FAKE_WEBMUX_LOG'])\n"
        "calls = json.loads(log.read_text()) if log.exists() else []\n"
        "calls.append({'argv': sys.argv[1:], 'stdin': sys.stdin.read()})\n"
        "log.write_text(json.dumps(calls))\n"
        "rows = json.loads(pathlib.Path(os.environ['FAKE_WEBMUX_RESPONSES']).read_text())\n"
        "rc, body = rows[len(calls) - 1]\n"
        "print(json.dumps(body))\n"
        "raise SystemExit(rc)\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "calls.json"
    monkeypatch.setenv("FAKE_WEBMUX_LOG", str(log))
    monkeypatch.setenv("FAKE_WEBMUX_RESPONSES", str(fixture))
    digest = hashlib.sha256(binary.read_bytes()).hexdigest()
    client = WorkerSlotClient(
        WorkerSlotConfig(binary.resolve(), digest, NODE, "fixture-owner", SESSION, INCARNATION)
    )
    return client, log, binary


def test_rejects_untrusted_binary(tmp_path):
    binary = tmp_path / "webmux"
    binary.write_text("x")
    binary.chmod(0o755)
    with pytest.raises(WorkerSlotError, match="fingerprint"):
        WorkerSlotClient(WorkerSlotConfig(binary, "0" * 64, NODE, "o", SESSION, INCARNATION))


def test_exact_guide_view_act_wire_and_queued_nonzero(tmp_path, monkeypatch):
    client, log, _binary = _client(
        tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (2, _queued())]
    )
    receipt = client.action(
        "enqueue-reservation",
        RESOURCE,
        "enqueue-0001",
        lease_seconds=120,
        wait_seconds=900,
    )
    assert receipt["ticket_id"] == TICKET
    assert receipt["queue"]["sequence"] == 42
    assert receipt["granted"] is False
    assert receipt["request_id"] == "enqueue-0001"
    calls = json.loads(log.read_text())
    assert [call["argv"] for call in calls] == [
        ["coordinate", "guide"],
        ["coordinate", "view"],
        ["coordinate", "act"],
    ]
    assert json.loads(calls[2]["stdin"])["action"] == "enqueue-reservation"


@pytest.mark.parametrize(
    ("view", "match"),
    [
        (_view(enabled=False), "authority"),
        (_view(self={"ref": SUBJECT, "active": False, "present": True}), "authority"),
        (
            _view(
                self={"ref": {**SUBJECT, "session_id": "other"}, "active": True, "present": True}
            ),
            "authority",
        ),
        (_view(history_epoch=""), "authority"),
        (_view(resources=[{**_view()["resources"][0], "capacity": True}]), "authority"),
        (_view(resources=[{**_view()["resources"][0], "capacity": 1.0}]), "authority"),
    ],
)
def test_identity_and_authority_refused_before_action(tmp_path, monkeypatch, view, match):
    client, log, _binary = _client(tmp_path, monkeypatch, [(0, _guide()), (0, view)])
    with pytest.raises(WorkerSlotError, match=match):
        client.action(
            "enqueue-reservation",
            RESOURCE,
            "enqueue-0001",
            lease_seconds=120,
            wait_seconds=900,
        )
    assert len(json.loads(log.read_text())) == 2


def test_acquire_requires_exact_current_live_top_level_lease(tmp_path, monkeypatch):
    grant = {
        "status": "reserved",
        "record_id": TICKET,
        "fence": 3,
        "expires": _future(60),
        "lease": {"authority_node_id": NODE, "history_epoch": "epoch-1", "current": True},
    }
    client, _log, _binary = _client(
        tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (0, grant)]
    )
    receipt = client.action("acquire-reservation", RESOURCE, "acquire-0001", record_id=TICKET)
    assert receipt["lease_id"] == TICKET
    assert receipt["fence"] == 3
    assert receipt["granted"] is True
    assert receipt["request_id"] == "acquire-0001"
    assert receipt["expires"]


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"record_id": "b" * 64}, "different lease record"),
        ({"fence": True}, "fence"),
        ({"status": "mystery"}, "confirm"),
        ({"expires": "2000-01-01T00:00:00Z"}, "safe requested interval"),
        ({"expires": _future(1000)}, "safe requested interval"),
        ({"lease": {"authority_node_id": NODE, "history_epoch": "epoch-1"}}, "current lease"),
        (
            {"lease": {"authority_node_id": NODE, "history_epoch": "epoch-1", "current": False}},
            "current lease",
        ),
        (
            {"lease": {"authority_node_id": NODE, "history_epoch": "stale", "current": True}},
            "epoch",
        ),
        (
            {"lease": {"authority_node_id": "other", "history_epoch": "epoch-1", "current": True}},
            "authority",
        ),
    ],
)
def test_unproven_acquire_is_rejected(tmp_path, monkeypatch, change, match):
    grant = {
        "status": "reserved",
        "record_id": TICKET,
        "fence": 3,
        "expires": _future(60),
        "lease": {"authority_node_id": NODE, "history_epoch": "epoch-1", "current": True},
        **change,
    }
    client, _log, _binary = _client(
        tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (0, grant)]
    )
    with pytest.raises(WorkerSlotError, match=match):
        client.action("acquire-reservation", RESOURCE, "acquire-0001", record_id=TICKET)


def test_queued_acquire_is_valid_non_grant(tmp_path, monkeypatch):
    client, _log, _binary = _client(
        tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (2, _queued())]
    )
    receipt = client.action("acquire-reservation", RESOURCE, "acquire-0001", record_id=TICKET)
    assert receipt["granted"] is False
    assert receipt["ticket_id"] == TICKET


def test_cancel_granted_does_not_claim_cancelled(tmp_path, monkeypatch):
    granted = _queued(status="queue-granted", state="granted")
    granted["queue"]["position"] = 0
    client, _log, _binary = _client(
        tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (2, granted)]
    )
    receipt = client.action("cancel-reservation", RESOURCE, "cancel-0001", record_id=TICKET)
    assert receipt["cancelled"] is False
    assert receipt["state"] == "granted"
    assert receipt["granted"] is False


@pytest.mark.parametrize(
    ("action", "receipt"),
    [
        ("enqueue-reservation", _queued(ready=True)),
        ("acquire-reservation", _queued(ready=True)),
        ("inspect-reservation", _queued(ready=True, status="queue-granted", state="granted")),
        ("inspect-reservation", _queued(status="queue-cancelled", state="cancelled")),
        ("inspect-reservation", _queued()),
    ],
)
def test_queue_receipt_rejects_incoherent_ready_position_and_action(
    tmp_path, monkeypatch, action, receipt
):
    if receipt["state"] == "waiting":
        receipt["queue"]["position"] = 0 if receipt["queue"]["ready"] else 1025
    else:
        receipt["queue"]["position"] = 1
        receipt["queue"]["ready"] = receipt["status"] == "queue-granted"
    fields = (
        {"lease_seconds": 120, "wait_seconds": 900}
        if action == "enqueue-reservation"
        else {"record_id": TICKET}
    )
    client, _log, _binary = _client(
        tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (2, receipt)]
    )
    with pytest.raises(WorkerSlotError, match="position evidence"):
        client.action(action, RESOURCE, "request-0001", **fields)


@pytest.mark.parametrize("action", ["renew-reservation", "release-reservation"])
@pytest.mark.parametrize(
    ("change", "match"),
    [({"fence": 4}, "different lease fence"), ({"record_id": "b" * 64}, "different lease record")],
)
def test_renew_and_release_reject_wrong_record_or_fence(
    tmp_path, monkeypatch, action, change, match
):
    response = {
        "status": "renewed" if action == "renew-reservation" else "released",
        "record_id": TICKET,
        "fence": 3,
        "expires": _future(60),
        "lease": {"authority_node_id": NODE, "history_epoch": "epoch-1", "current": True},
        **change,
    }
    fields = {"record_id": TICKET, "fence": 3}
    if action == "renew-reservation":
        fields["lease_seconds"] = 120
    client, _log, _binary = _client(
        tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (0, response)]
    )
    with pytest.raises(WorkerSlotError, match=match):
        client.action(action, RESOURCE, "transition-0001", **fields)


def test_unknown_or_incoherent_queue_status_is_rejected(tmp_path, monkeypatch):
    client, _log, _binary = _client(
        tmp_path,
        monkeypatch,
        [(0, _guide()), (0, _view()), (2, {"status": "mystery", "queue": {}})],
    )
    with pytest.raises(WorkerSlotError, match="queue action failed"):
        client.action("inspect-reservation", RESOURCE, "inspect-0001", record_id=TICKET)


def test_wait_is_observer_only_and_uses_exact_argv(tmp_path, monkeypatch):
    client, log, _binary = _client(
        tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (1, _queued(ready=True))]
    )
    receipt = client.wait(RESOURCE, TICKET, "wait-0001", 1)
    assert receipt["granted"] is False
    assert receipt["queue"]["ready"] is True
    assert receipt["request_id"] == "wait-0001"
    assert json.loads(log.read_text())[2]["argv"] == [
        "coordinate",
        "wait",
        "--resource",
        RESOURCE,
        "--record-id",
        TICKET,
        "--request-id",
        "wait-0001",
        "--timeout-seconds",
        "1",
    ]


def test_binary_is_rechecked_before_action(tmp_path, monkeypatch):
    client, _log, binary = _client(tmp_path, monkeypatch, [])
    binary.write_text("changed")
    binary.chmod(0o755)
    with pytest.raises(WorkerSlotError, match="fingerprint"):
        client.action(
            "enqueue-reservation",
            RESOURCE,
            "enqueue-0001",
            lease_seconds=120,
            wait_seconds=900,
        )


def test_secret_child_output_is_never_returned(tmp_path, monkeypatch):
    bad = {"status": "queued", "token": "secret", "reason": "secret"}
    client, _log, _binary = _client(tmp_path, monkeypatch, [(0, _guide()), (0, _view()), (2, bad)])
    with pytest.raises(WorkerSlotError) as exc:
        client.action("inspect-reservation", RESOURCE, "inspect-0001", record_id=TICKET)
    assert "secret" not in str(exc.value)


class _ActualCliHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []
    acquisition_receipts: dict[str, dict] = {}
    grant_commits = 0
    drop_request_id: str | None = None
    released = False
    ready = True

    def log_message(self, *_args):
        pass

    def do_GET(self):
        body = _view()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        request = json.loads(self.rfile.read(length))
        self.calls.append(request)
        action = request["action"]
        request_id = request["request_id"]
        if action == "enqueue-reservation":
            body = _queued()
        elif action == "inspect-reservation":
            body = _queued(ready=self.ready)
        elif action == "acquire-reservation":
            body = self.acquisition_receipts.get(request_id)
            if body is None:
                if not self.ready:
                    body = _queued()
                else:
                    type(self).grant_commits += 1
                    body = {
                        "status": "reserved",
                        "record_id": TICKET,
                        "fence": 7,
                        "expires": _future(120),
                        "lease": {
                            "authority_node_id": NODE,
                            "history_epoch": "epoch-1",
                            "current": True,
                        },
                    }
                self.acquisition_receipts[request_id] = body
            if body.get("lease"):
                body = {**body, "lease": {**body["lease"], "current": not self.released}}
            if (
                request_id == self.drop_request_id
                and sum(call.get("request_id") == request_id for call in self.calls) == 1
            ):
                self.close_connection = True
                return
        elif action == "renew-reservation":
            body = {
                "status": "renewed",
                "record_id": TICKET,
                "fence": 7,
                "expires": _future(request["lease_seconds"]),
                "lease": {
                    "authority_node_id": NODE,
                    "history_epoch": "epoch-1",
                    "current": True,
                },
            }
        elif action == "release-reservation":
            type(self).released = True
            body = {"status": "released", "record_id": TICKET, "fence": 7}
        else:
            body = {"status": "unknown"}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())


def test_qualified_real_webmux_cli_wire(tmp_path, monkeypatch):
    binary_env = os.environ.get("MAC_WEBMUX_TEST_BINARY")
    if not binary_env:
        pytest.skip("qualified upstream WebMux fixture not staged")
    binary = Path(binary_env).resolve()
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == os.environ["MAC_WEBMUX_TEST_SHA256"]
    _ActualCliHandler.calls = []
    _ActualCliHandler.acquisition_receipts = {}
    _ActualCliHandler.grant_commits = 0
    _ActualCliHandler.drop_request_id = "acquire-actual-0001"
    _ActualCliHandler.released = False
    _ActualCliHandler.ready = True
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ActualCliHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    access = tmp_path / "access.json"
    access.write_text(
        json.dumps(
            {
                "contract": "webmux-session-coordination/v1",
                "endpoint": f"http://127.0.0.1:{server.server_port}",
                "capability": "wmc1.fixture.private",
                "subject": SUBJECT,
                "instructions": [],
            }
        )
    )
    access.chmod(0o600)
    monkeypatch.setenv("WEBMUX_COORDINATION_FILE", str(access))
    try:
        client = WorkerSlotClient(
            WorkerSlotConfig(
                binary,
                os.environ["MAC_WEBMUX_TEST_SHA256"],
                NODE,
                "fixture-owner",
                SESSION,
                INCARNATION,
            )
        )
        receipt = client.action(
            "enqueue-reservation",
            RESOURCE,
            "enqueue-actual-0001",
            lease_seconds=120,
            wait_seconds=900,
        )
        observed = client.wait(RESOURCE, TICKET, "wait-actual-0001", 1)
        with pytest.raises(WorkerSlotError):
            client.action("acquire-reservation", RESOURCE, "acquire-actual-0001", record_id=TICKET)
        acquired = client.action(
            "acquire-reservation", RESOURCE, "acquire-actual-0001", record_id=TICKET
        )
        renewed = client.action(
            "renew-reservation",
            RESOURCE,
            "renew-actual-0001",
            record_id=TICKET,
            fence=7,
            lease_seconds=120,
        )
        released = client.action(
            "release-reservation",
            RESOURCE,
            "release-actual-0001",
            record_id=TICKET,
            fence=7,
        )
        with pytest.raises(WorkerSlotError, match="current lease"):
            client.action("acquire-reservation", RESOURCE, "acquire-actual-0001", record_id=TICKET)
    finally:
        server.shutdown()
        server.server_close()
    assert receipt["ticket_id"] == TICKET
    assert observed["granted"] is False
    assert acquired["request_id"] == "acquire-actual-0001"
    assert renewed["status"] == "renewed"
    assert released["status"] == "released"
    assert _ActualCliHandler.grant_commits == 1
    assert [call["action"] for call in _ActualCliHandler.calls] == [
        "enqueue-reservation",
        "inspect-reservation",
        "acquire-reservation",
        "acquire-reservation",
        "renew-reservation",
        "release-reservation",
        "acquire-reservation",
    ]


def test_qualified_real_webmux_cli_frozen_queued_acquire_does_not_later_grant(
    tmp_path, monkeypatch
):
    binary_env = os.environ.get("MAC_WEBMUX_TEST_BINARY")
    if not binary_env:
        pytest.skip("qualified upstream WebMux fixture not staged")
    binary = Path(binary_env).resolve()
    _ActualCliHandler.calls = []
    _ActualCliHandler.acquisition_receipts = {}
    _ActualCliHandler.grant_commits = 0
    _ActualCliHandler.drop_request_id = None
    _ActualCliHandler.released = False
    _ActualCliHandler.ready = False
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ActualCliHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    access = tmp_path / "access.json"
    access.write_text(
        json.dumps(
            {
                "contract": "webmux-session-coordination/v1",
                "endpoint": f"http://127.0.0.1:{server.server_port}",
                "capability": "wmc1.fixture.private",
                "subject": SUBJECT,
                "instructions": [],
            }
        )
    )
    access.chmod(0o600)
    monkeypatch.setenv("WEBMUX_COORDINATION_FILE", str(access))
    try:
        client = WorkerSlotClient(
            WorkerSlotConfig(
                binary,
                os.environ["MAC_WEBMUX_TEST_SHA256"],
                NODE,
                "fixture-owner",
                SESSION,
                INCARNATION,
            )
        )
        first = client.action(
            "acquire-reservation", RESOURCE, "acquire-frozen-0001", record_id=TICKET
        )
        _ActualCliHandler.ready = True
        replay = client.action(
            "acquire-reservation", RESOURCE, "acquire-frozen-0001", record_id=TICKET
        )
    finally:
        server.shutdown()
        server.server_close()
    assert first["granted"] is replay["granted"] is False
    assert first["request_id"] == replay["request_id"] == "acquire-frozen-0001"
    assert _ActualCliHandler.grant_commits == 0
