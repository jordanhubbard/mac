"""Real process controls for startup, progress loss, restart and bounded shutdown."""

import json
import os
from pathlib import Path
import plistlib
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

import pytest

from mac import world4d_service as service


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(port, path="/health"):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as response:
        return response.read().decode()


def eventually(fn, timeout=12):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            result = fn()
            if result:
                return result
        except (OSError, ValueError, urllib.error.URLError):
            pass
        time.sleep(0.05)
    raise AssertionError("condition did not become true")


@pytest.fixture
def config(tmp_path):
    source = tmp_path / "external source"
    source.mkdir()
    (source / "world4d.py").write_text("")
    (source / "world4d_slack_post.py").write_text(
        "def build_post(world, port):\n    return f'{world} snapshot on {port}'\n"
        "def main():\n    raise AssertionError('must never post directly')\n"
    )
    (source / "world4d_daemon.py").write_text("""
import argparse, json, time
from http.server import BaseHTTPRequestHandler, HTTPServer
p=argparse.ArgumentParser()
p.add_argument('--world'); p.add_argument('--port',type=int); p.add_argument('--hz')
a=p.parse_args(); start=time.monotonic(); frozen=None
class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def do_GET(self):
        global frozen
        tick=int((time.monotonic()-start)*10) if frozen is None else frozen
        if self.path=='/freeze': frozen=tick
        body=json.dumps({'tick':tick,'paused':False,'objects':[{'kind':'agent'}]}).encode()
        self.send_response(200); self.end_headers(); self.wfile.write(body)
HTTPServer(('127.0.0.1',a.port),Handler).serve_forever()
""")
    env = tmp_path / "environment.json"
    env.write_text(json.dumps({"PATH": os.environ["PATH"]}))
    env.chmod(0o600)
    root = tmp_path / "service"
    root.mkdir()
    ports = set()
    while len(ports) < 3:
        ports.add(free_port())
    monitor, first, second = sorted(ports)
    return {
        "root": str(root),
        "source": str(source),
        "python": sys.executable,
        "environment_file": str(env),
        "monitor_port": monitor,
        "worlds": [{"name": "test", "port": first}, {"name": "fleet", "port": second}],
        "stall_seconds": 0.7,
        "poll_seconds": 0.05,
        "user": "example",
        "home": str(tmp_path),
    }


def launch(config):
    path = Path(config["root"]) / "config.json"
    path.write_text(json.dumps(config))
    return subprocess.Popen([sys.executable, service.__file__, "run", str(path)])


def test_runtime_restarts_failed_and_stalled_children_and_stops_cleanly(config):
    proc = launch(config)
    known_pids = set()
    port = config["monitor_port"]
    try:
        status = eventually(lambda: json.loads(get(port)))
        assert status["ready"]
        known_pids.update(w["pid"] for w in status["worlds"])
        baseline = get(port, "/monitor")
        time.sleep(0.2)
        assert get(port, "/monitor") == baseline
        snapshot = get(port, "/snapshot")
        assert "test snapshot" in snapshot and "fleet snapshot" in snapshot
        assert "\n\n" in snapshot
        first_pid = status["worlds"][0]["pid"]
        os.kill(first_pid, signal.SIGKILL)

        def restarted():
            data = json.loads(get(port))
            return data if data["ready"] and data["worlds"][0]["pid"] != first_pid else None

        status = eventually(restarted)
        assert status["worlds"][0]["restarts"] == 1
        known_pids.add(status["worlds"][0]["pid"])
        second_pid = status["worlds"][1]["pid"]
        get(config["worlds"][1]["port"], "/freeze")
        status = eventually(
            lambda: (
                (d if d["ready"] and d["worlds"][1]["pid"] != second_pid else None)
                if (d := json.loads(get(port)))
                else None
            )
        )
        known_pids.add(status["worlds"][1]["pid"])
        assert status["worlds"][1]["restarts"] == 1
    finally:
        proc.terminate()
        assert proc.wait(timeout=10) == 0
    for pid in known_pids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


def test_unready_worlds_fail_monitor_and_snapshot(config):
    world = service.World(config["worlds"][0], config)
    server = service.ThreadingHTTPServer(("127.0.0.1", 0), service.handler_for([world], config))
    thread = service.threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        for path in ("/health", "/monitor", "/snapshot"):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(server.server_port, path)
            assert error.value.code == 503
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_prepare_pins_runtime_and_generates_boot_service(config):
    path = Path(config["root"]) / "input.json"
    path.write_text(json.dumps(config))
    plist_path = service.prepare(path)
    plist = plistlib.loads(plist_path.read_bytes())
    assert plist["RunAtLoad"] and plist["KeepAlive"]
    assert plist["UserName"] == "example"
    installed = json.loads((Path(config["root"]) / "service.json").read_text())
    pinned = Path(installed["source"])
    assert (pinned / "manifest.json").exists()
    (Path(config["source"]) / "world4d.py").write_text("# later source edit")
    assert (pinned / "world4d.py").read_text() == ""
    (pinned / "world4d.py").write_text("tampered")
    # Use original input bytes so this addresses the same pinned generation.
    (Path(config["source"]) / "world4d.py").write_text("")
    with pytest.raises(ValueError, match="pinned runtime has changed"):
        service.prepare(path)


def test_environment_rejects_world_readable_secrets(config):
    path = Path(config["environment_file"])
    path.chmod(0o644)
    with pytest.raises(ValueError, match="0600"):
        service.load_environment(str(path))


def test_install_refuses_ambiguous_ports_and_unsafe_log_names(config):
    config["worlds"][1]["port"] = config["worlds"][0]["port"]
    with pytest.raises(ValueError, match="ports must be distinct"):
        service.validate_config(config)
    config["worlds"][1]["port"] = free_port()
    config["worlds"][0]["name"] = "../other-service"
    with pytest.raises(ValueError, match="simple identifiers"):
        service.validate_config(config)
