from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OBSERVER_PATH = ROOT / "deploy" / "mac-crash-observer.py"


def _load_observer():
    spec = importlib.util.spec_from_file_location("mac_crash_observer_test", OBSERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_observer_captures_trace_and_posts_before_returning_failure(monkeypatch, tmp_path):
    observer = _load_observer()
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def do_GET(self):
            body = json.dumps({"current_task_id": "task_crash", "lease_id": "lease_crash"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "mac-home"))
    monkeypatch.setenv("MAC_HUB_URL", "http://127.0.0.1:%d" % server.server_port)
    monkeypatch.setenv("MAC_WORKER_TOKEN", "secret")
    monkeypatch.setenv("MAC_AGENT_ID", "agent_test")
    monkeypatch.setattr(observer, "_enable_core_dumps", lambda: {"enabled": True})
    monkeypatch.setattr(observer, "_core_evidence", lambda *_a: ("core:test", {"provider": "test"}, ""))
    try:
        rc = observer.observe(
            "systemd",
            ["/usr/bin/env", "python3", "-c", "raise RuntimeError('observer proof')"],
        )
    finally:
        server.shutdown()
        server.server_close()
    assert rc == 1
    assert len(received) == 1
    payload = received[0]
    assert payload["schema"] == "mac.agent_crash_occurrence.v1"
    assert payload["task_id"] == "task_crash"
    assert payload["core_reference"] == "core:test"
    assert "RuntimeError: observer proof" in payload["stack_trace"]
    assert list((tmp_path / "mac-home" / "crash-spool").glob("*.json")) == []


def test_observer_spool_is_mode_0600_and_replayed(monkeypatch, tmp_path):
    observer = _load_observer()
    spool = tmp_path / "spool"
    payload = {"event_id": "evt-spooled", "reason": "offline"}
    path = observer._spool_payload(spool, payload)
    assert path.stat().st_mode & 0o777 == 0o600
    sent = []
    monkeypatch.setattr(
        observer,
        "_post_report",
        lambda _url, _token, _agent, value: sent.append(value) or True,
    )
    assert observer._flush_spool(spool, "http://hub", "token", "agent_a") == 1
    assert sent == [payload]
    assert not path.exists()


def test_every_deployment_supervisor_uses_external_crash_observer():
    deploy = (
        (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    )
    assert "ulimit -c unlimited" in deploy
    assert "export PYTHONFAULTHANDLER=1" in deploy
    for supervisor in ("systemd", "supervisord", "launchd"):
        assert "--supervisor %s" % supervisor in deploy or (
            "<string>--supervisor</string><string>%s</string>" % supervisor
        ) in deploy
    assert deploy.count("mac-crash-observer") >= 4

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "PYTHONFAULTHANDLER=1" in dockerfile
    assert "deploy/mac-crash-observer.py /usr/local/bin/mac-crash-observer" in dockerfile

    manifest = (ROOT / "deploy" / "k8s" / "mac-runner" / "deployment.yaml").read_text(
        encoding="utf-8"
    )
    assert "/usr/local/bin/mac-crash-observer" in manifest
    assert "- kubernetes" in manifest
    assert "MAC_CRASH_SPOOL_DIR" in manifest
    assert "mountPath: /var/lib/mac/crash-spool" in manifest
    assert "runAsUser: 10001" in manifest
    assert "runAsGroup: 10001" in manifest
    assert "fsGroup: 10001" in manifest


def test_observer_forwards_termination_and_reaps_child(tmp_path):
    ready = tmp_path / "ready"
    signal_seen = tmp_path / "signal"
    child_code = """
import os
import signal
import sys
import time

ready, signal_seen = sys.argv[1:]

def stop(signum, _frame):
    with open(signal_seen, "w", encoding="utf-8") as stream:
        stream.write(str(signum))
    raise SystemExit(0)

signal.signal(signal.SIGTERM, stop)
with open(ready, "w", encoding="utf-8") as stream:
    stream.write(str(os.getpid()))
while True:
    time.sleep(0.1)
"""
    observer = subprocess.Popen(
        [
            sys.executable,
            str(OBSERVER_PATH),
            "--supervisor",
            "launchd",
            "--",
            sys.executable,
            "-c",
            child_code,
            str(ready),
            str(signal_seen),
        ],
        env={**os.environ, "MAC_HOME": str(tmp_path / "mac-home")},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    child_pid = None
    try:
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            assert observer.poll() is None
            time.sleep(0.05)
        assert ready.exists()
        child_pid = int(ready.read_text(encoding="utf-8"))

        observer.send_signal(signal.SIGTERM)
        stdout, stderr = observer.communicate(timeout=10)
        assert observer.returncode == 0, (stdout, stderr)
        assert signal_seen.read_text(encoding="utf-8") == str(signal.SIGTERM)
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise AssertionError("crash observer returned before its managed child exited")
    finally:
        if observer.poll() is None:
            observer.kill()
            observer.wait(timeout=5)
        if child_pid is not None:
            try:
                os.kill(child_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_filesystem_core_is_retained_with_bounded_permissions(tmp_path):
    observer = _load_observer()
    source = tmp_path / "core.99"
    source.write_bytes(b"core-bytes")
    retained, metadata = observer._retain_core(
        str(source),
        {"provider": "filesystem"},
        tmp_path / "mac-home",
        "event-99",
        {"MAC_CRASH_CORE_MAX_BYTES": "100", "MAC_CRASH_CORE_RETAIN_COUNT": "2"},
    )
    retained_path = Path(retained)
    assert retained_path.read_bytes() == b"core-bytes"
    assert retained_path.stat().st_mode & 0o777 == 0o600
    assert metadata["retained"] is True
