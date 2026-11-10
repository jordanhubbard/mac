"""Regression coverage for stale presence and mixed-store client deployment."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
import tempfile

import pytest

from mac import world4d_client as client


def test_live_pid_is_not_enough_and_reused_or_stale_pid_is_not_online(tmp_path):
    # Darwin Unix socket paths have a 104-byte limit; pytest's root can exceed it.
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="w4-") as short:
        _probe_liveness(Path(short))


def _probe_liveness(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    born = subprocess.check_output(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="],
        text=True,
        env={**os.environ, "LC_ALL": "C"},
    ).strip()
    heartbeat = {
        "pid": os.getpid(),
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "start_time": datetime.strptime(born, "%a %b %d %H:%M:%S %Y").timestamp(),
        "loop_tick_socket": True,
    }
    path = state / "gateway.heartbeat"
    path.write_text(json.dumps(heartbeat))
    assert client.gateway_health(tmp_path) == "unknown"  # No live event loop.
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(state / f"gateway.loop-tick.{os.getpid()}.sock"))
    sock.listen()

    def serve():
        with sock.accept()[0] as conn:
            conn.sendall(b"1")

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert client.gateway_health(tmp_path) == "online"
        thread.join(timeout=2)
        path.write_text(json.dumps({**heartbeat, "start_time": heartbeat["start_time"] - 100}))
        assert client.gateway_health(tmp_path) == "unknown"
        path.write_text(json.dumps(heartbeat))
        assert (
            client.gateway_health(tmp_path, now=datetime.now(timezone.utc).timestamp() + 120)
            == "unknown"
        )
    finally:
        sock.close()


def test_audit_catches_legacy_report_store_missing_skill_and_competing_server(tmp_path):
    manifest = {
        "command": ["python", "canonical-adapter.py"],
        "work_jobs": ["work"],
        "report_jobs": ["report"],
        "retired_servers": ["world"],
        "monitor_url": "http://mesh:7481/mac/world4d/snapshot-on-change",
        "python": "python",
        "client_module": "manager.py",
        "manifest_path": "manifest.json",
        "refresh_launcher": str(tmp_path / "fleet-context"),
        "hermes_home": str(tmp_path),
    }
    config = {
        "mcp_servers": {
            "world4d": {"command": "python", "args": ["canonical-adapter.py"]},
            "world": {"enabled": True},
        }
    }
    jobs = [{"id": "work", "skills": []}, {"id": "report", "prompt": "query world4d_objects"}]
    hook = tmp_path / "hook"
    Path(manifest["refresh_launcher"]).write_text("bin/mac-world4d-presence")
    skill = tmp_path / "skills/world4d-coordination/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("coordination guidance")
    errors = client.audit(manifest, config, jobs, hook)
    assert len(errors) == 4
    config["mcp_servers"]["world"]["enabled"] = False
    jobs[0]["skills"] = ["world4d-coordination"]
    jobs[1].update(client.report_patch(manifest))
    hook.write_text(client.launcher(manifest))
    hook.chmod(0o700)
    assert client.audit(manifest, config, jobs, hook) == []
    hook.write_text("#!/bin/sh\nexit 0\n")
    assert client.audit(manifest, config, jobs, hook) == ["runtime presence hook differs"]


def test_realm_validation_is_readonly_and_refuses_different_store(monkeypatch):
    manifest = {"realm_id": "one", "world": "fleet", "backend": "prototype", "command": ["adapter"]}
    calls = []

    def rpc(command, method):
        calls.append(method)
        return {"realm_id": "two", "world": "fleet", "backend": "prototype"}

    monkeypatch.setattr(client, "rpc", rpc)
    with pytest.raises(ValueError, match="different realm"):
        client.verify_realm(manifest)
    assert calls == ["mac/world4d_identity"]


def test_native_api_migration_preserves_schedule_and_origin_and_backs_up(tmp_path, monkeypatch):
    import sys
    import types
    import yaml

    manifest = {
        "realm_id": "canonical",
        "agent": "agent_bullwinkle",
        "world": "fleet",
        "backend": "prototype",
        "command": ["ssh", "adapter"],
        "hermes_home": str(tmp_path / "hermes"),
        "manifest_path": str(tmp_path / "mac/client.json"),
        "python": "python",
        "client_module": "manager.py",
        "hook": str(tmp_path / "mac/bin/mac-world4d-presence"),
        "refresh_launcher": str(tmp_path / "refresh"),
        "work_jobs": ["work"],
        "report_jobs": ["report"],
        "retired_servers": ["world"],
        "monitor_url": "http://canonical/snapshot-on-change",
    }
    home = Path(manifest["hermes_home"])
    skill = home / "skills/world4d-coordination/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("shared-world guidance")
    Path(manifest["refresh_launcher"]).write_text("bin/mac-world4d-presence")
    config = {
        "mcp_servers": {"world": {"command": "old-store"}, "unrelated": {"command": "keep"}},
        "model": "retain",
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    original = (home / "config.yaml").read_bytes()
    jobs = {
        "work": {"id": "work", "skills": ["existing"], "prompt": "do work"},
        "report": {
            "id": "report",
            "prompt": "query world4d_objects",
            "schedule": {"minutes": 30},
            "origin": {"platform": "slack", "chat_id": "original"},
            "deliver": "local",
        },
    }
    api = types.ModuleType("cron.jobs")
    api.get_job = jobs.get
    api.load_jobs = lambda: list(jobs.values())

    def update(job_id, patch):
        jobs[job_id].update(patch)
        return jobs[job_id]

    api.update_job = update
    monkeypatch.setitem(sys.modules, "cron", types.ModuleType("cron"))
    monkeypatch.setitem(sys.modules, "cron.jobs", api)
    monkeypatch.setattr(client, "verify_realm", lambda m: None)
    result = client.install(manifest)
    assert (Path(result["backup"]) / "config.yaml").read_bytes() == original
    after = yaml.safe_load((home / "config.yaml").read_text())
    assert after["model"] == "retain"
    assert after["mcp_servers"]["unrelated"] == {"command": "keep"}
    assert after["mcp_servers"]["world"]["enabled"] is False
    assert jobs["work"]["skills"] == ["existing", "world4d-coordination"]
    assert jobs["report"]["schedule"] == {"minutes": 30}
    assert jobs["report"]["origin"]["chat_id"] == "original"
    assert jobs["report"]["deliver"] == "origin"
    assert jobs["report"]["monitor_url"] == manifest["monitor_url"]
    assert not client.audit(manifest, after, api.load_jobs(), Path(manifest["hook"]))
