"""Unit contracts for worker entrypoint modes and executor failure reporting."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import worker


class FakeClient:
    def __init__(self, url, token=None):
        self.url = url
        self.token = token
        self.posts = []
        self.requests = []

    def request(self, method, path, body):
        self.requests.append((method, path, body))
        return {"ok": True}

    def post(self, path, body):
        self.posts.append((path, body))
        if path.endswith("attestation-key/rotate"):
            return {"attestation_key": "r" * 40}
        if path.endswith("heartbeat"):
            return {"id": "agent_test", "status": "idle"}
        return {"ok": True}


class FakeResult:
    def __init__(self, status="idle"):
        self.status = status

    def to_dict(self):
        return {"status": self.status}


class FakeWorker:
    instances = []
    install_ok = True
    run_status = "idle"
    loop_statuses = ["idle"]

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.__class__.instances.append(self)

    def ensure_pip(self, specs, **kwargs):
        return {"ok": self.install_ok, "specs": specs, **kwargs}

    def ensure_npm(self, packages, **kwargs):
        return {"ok": self.install_ok, "packages": packages, **kwargs}

    def dry_run_claim(self):
        return {"task": "candidate"}

    def run_once(self):
        return FakeResult(self.run_status)

    def run_forever(self, max_iterations=None):
        self.max_iterations = max_iterations
        return [FakeResult(status) for status in self.loop_statuses]


@pytest.fixture()
def worker_main(monkeypatch):
    clients = []

    def client_factory(url, token=None):
        client = FakeClient(url, token)
        clients.append(client)
        return client

    FakeWorker.instances = []
    FakeWorker.install_ok = True
    FakeWorker.run_status = "idle"
    FakeWorker.loop_statuses = ["idle"]
    monkeypatch.setattr(worker, "MacApiClient", client_factory)
    monkeypatch.setattr(worker, "MacWorker", FakeWorker)
    monkeypatch.setattr(worker, "SubprocessExecutor", lambda argv, timeout=None: {"argv": argv, "timeout": timeout})
    monkeypatch.delenv("MAC_ATTESTATION_KEY", raising=False)
    monkeypatch.delenv("MAC_TOKEN", raising=False)
    monkeypatch.delenv("MAC_WORKER_TOKEN", raising=False)
    monkeypatch.delenv("MAC_API_TOKEN", raising=False)
    monkeypatch.delenv("MAC_AGENT_ID", raising=False)
    return clients


def test_worker_main_requires_identity_and_executor(worker_main, capsys):
    assert worker.main([]) == 1
    assert "--agent-id or --register" in capsys.readouterr().out
    assert worker.main(["--agent-id", "agent_test"]) == 1
    assert "--executor is required" in capsys.readouterr().out


def test_worker_main_resolves_fleet_token_and_heartbeat(worker_main, monkeypatch, capsys):
    monkeypatch.setattr("mac.fleet_env.resolve_first", lambda names, fleet=None: "fleet-token" if fleet else None)
    assert worker.main(
        ["--fleet", "rocky", "--agent-id", "agent_test", "--heartbeat-only", "--running-digest", "sha256:test"]
    ) == 0
    assert worker_main[-1].token == "fleet-token"
    assert worker_main[-1].posts[-1][1]["running_digest"] == "sha256:test"
    assert json.loads(capsys.readouterr().out)["status"] == "heartbeat"


def test_worker_main_registers_and_persists_attestation_key(
    worker_main, monkeypatch, tmp_path, capsys
):
    # worker.main exports the freshly issued key for its child executor. Track
    # the key through monkeypatch so this in-process CLI test restores the
    # parent environment instead of leaking a fake key to later subprocesses.
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "")
    env_file = tmp_path / "agent" / "mac.env"
    monkeypatch.setattr(
        worker,
        "register_worker",
        lambda *_args, **_kwargs: {
            "id": "agent_registered",
            "attestation_key": "a" * 40,
            "resources": {"openshell_required": True},
        },
    )
    assert worker.main(
        [
            "--register",
            "--hostname",
            "host",
            "--agent-name",
            "registered",
            "--capabilities",
            "python, ops",
            "--resources",
            '{"cpu": 4}',
            "--attestation-key-env",
            str(env_file),
            "--heartbeat-only",
        ]
    ) == 0
    assert "MAC_ATTESTATION_KEY=" + ("a" * 40) in env_file.read_text(encoding="utf-8")
    assert worker.os.environ["MAC_ATTESTATION_KEY"] == "a" * 40
    assert json.loads(capsys.readouterr().out)["registered"]["id"] == "agent_registered"


@pytest.mark.parametrize("ok,expected", [(True, 0), (False, 1)])
def test_worker_main_self_install_modes(worker_main, capsys, ok, expected):
    FakeWorker.install_ok = ok
    assert worker.main(
        [
            "--agent-id",
            "agent_test",
            "--install-pip",
            "ruff",
            "--install-npm",
            "typescript",
            "--install-index-url",
            "https://packages.example/simple",
            "--install-reason",
            "test",
        ]
    ) == expected
    payload = json.loads(capsys.readouterr().out)
    assert set(payload["results"]) == {"pip", "npm"}


def test_worker_main_rotates_missing_and_invalid_attestation_keys(
    worker_main, monkeypatch, tmp_path
):
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "")
    env_file = tmp_path / "mac.env"
    assert worker.main(
        [
            "--agent-id",
            "agent_test",
            "--rotate-missing-attestation-key",
            "--attestation-key-env",
            str(env_file),
            "--heartbeat-only",
        ]
    ) == 0
    assert any(path.endswith("attestation-key/rotate") for path, _ in worker_main[-1].posts)

    monkeypatch.setenv("MAC_ATTESTATION_KEY", "old-key")
    monkeypatch.setattr(worker, "_attestation_key_matches_hub", lambda *_args: False)
    assert worker.main(
        ["--agent-id", "agent_test", "--rotate-invalid-attestation-key", "--heartbeat-only"]
    ) == 0
    assert any(path.endswith("attestation-key/rotate") for path, _ in worker_main[-1].posts)

    monkeypatch.setenv("MAC_ATTESTATION_KEY", "current-key")
    monkeypatch.setattr(worker, "_attestation_key_matches_hub", lambda *_args: True)
    assert worker.main(
        ["--agent-id", "agent_test", "--rotate-invalid-attestation-key", "--heartbeat-only"]
    ) == 0
    assert not any(path.endswith("attestation-key/rotate") for path, _ in worker_main[-1].posts)


def test_worker_main_dry_run_and_executor_modes(worker_main, tmp_path, capsys):
    assert worker.main(
        [
            "--agent-id",
            "agent_test",
            "--workspace",
            str(tmp_path),
            "--allowed-projects",
            "one,two",
            "--required-metadata",
            '{"canary": true}',
            "--require-canary",
            "--disable-agentbus-control",
            "--self-update-repo",
            str(tmp_path),
            "--dry-run-claim",
        ]
    ) == 0
    assert json.loads(capsys.readouterr().out)["assignment"] == {"task": "candidate"}
    instance = FakeWorker.instances[-1]
    assert instance.kwargs["allowed_projects"] == ["one", "two"]
    assert instance.kwargs["required_metadata"] == {"canary": True}
    assert instance.kwargs["agentbus_control_enabled"] is False

    FakeWorker.run_status = "self_update_restart"
    assert worker.main(["--agent-id", "agent_test", "--executor", "runner"]) == 75
    assert json.loads(capsys.readouterr().out)["status"] == "self_update_restart"
    FakeWorker.run_status = "completed"
    assert worker.main(["--agent-id", "agent_test", "--executor", "runner"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"

    FakeWorker.loop_statuses = ["idle", "self_update_restart"]
    assert worker.main(
        ["--agent-id", "agent_test", "--loop", "--max-iterations", "2", "--executor", "runner"]
    ) == 75
    assert len(json.loads(capsys.readouterr().out)) == 2
    FakeWorker.loop_statuses = ["idle"]
    assert worker.main(["--agent-id", "agent_test", "--loop", "--executor", "runner"]) == 0


def test_worker_env_file_helpers_replace_append_and_ignore_comments(tmp_path):
    path = tmp_path / "nested" / "mac.env"
    assert worker._read_env_value(path, "KEY") is None
    path.parent.mkdir()
    path.write_text("# KEY=ignored\nBROKEN\nEMPTY=\nKEY=old\n", encoding="utf-8")
    assert worker._read_env_value(path, "KEY") == "old"
    assert worker._read_env_value(path, "EMPTY") is None
    worker._write_env_value(path, "KEY", "new")
    worker._write_env_value(path, "ADDED", "value")
    content = path.read_text(encoding="utf-8")
    assert "KEY=new" in content
    assert "ADDED=value" in content
    assert path.stat().st_mode & 0o777 == 0o600


def test_subprocess_executor_audits_timeout_oserror_and_sink_failure(
    tmp_path, monkeypatch
):
    task = {"id": "task_1"}
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "task.json").write_text("{}", encoding="utf-8")
    executor = worker.SubprocessExecutor(["runner", "--token", "secret"], timeout=2)
    audit = []
    executor.audit_sink = audit.append
    executor.audit_context = {"task_id": "task_1", "lease_id": "lease_1"}

    class TimeoutProcess:
        pid = 999999
        returncode = -9

        def __init__(self, *_args, **kwargs):
            kwargs["stdout"].write("partial")
            kwargs["stderr"].write("late")
            self.wait_count = 0

        def wait(self, timeout=None):
            self.wait_count += 1
            if self.wait_count == 1:
                raise subprocess.TimeoutExpired("runner", timeout)
            return self.returncode

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(worker.subprocess, "Popen", TimeoutProcess)
    monkeypatch.setattr(worker, "_terminate_process_tree", lambda *_args, **_kwargs: None)
    with pytest.raises(subprocess.TimeoutExpired):
        executor(task, task_dir)
    assert audit[-1]["phase"] == "timeout"
    assert audit[-1]["stdout_bytes"] == len("partial")

    monkeypatch.setattr(
        worker.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("missing executable")),
    )
    with pytest.raises(OSError, match="missing executable"):
        executor(task, task_dir)
    assert audit[-1]["phase"] == "error"

    executor.audit_sink = lambda _record: (_ for _ in ()).throw(RuntimeError("ignored"))
    executor._emit_audit({"phase": "test"})


def test_worker_argument_helpers_and_attestation_probe(monkeypatch):
    assert worker._csv_arg(None) == []
    assert worker._csv_arg(" one, ,two ") == ["one", "two"]
    assert worker._json_arg(None) == {}
    assert worker._json_arg('{"ok": true}') == {"ok": True}
    with pytest.raises(worker.MacApiError):
        worker._json_arg("[]")
    monkeypatch.setenv("BOOL", "yes")
    assert worker._env_bool("BOOL") is True
    monkeypatch.setenv("BOOL", "")
    assert worker._env_bool("BOOL", True) is True

    client = FakeClient("https://hub")
    client.post = lambda *_args, **_kwargs: {"valid": True}
    assert worker._attestation_key_matches_hub(client, "agent", "k" * 40)
    client.post = lambda *_args, **_kwargs: []
    assert not worker._attestation_key_matches_hub(client, "agent", "k" * 40)


# ---------------------------------------------------------------------------
# Helper shared by startup-behavior tests
# ---------------------------------------------------------------------------

def _fake_register():
    """Return a minimal registration payload that satisfies worker.main."""
    return {"id": "agent_startup_test", "attestation_key": "", "resources": {}}


def _register_args():
    """Common CLI flags for a --register + --heartbeat-only run."""
    return ["--register", "--hostname", "host", "--agent-name", "test", "--heartbeat-only"]


# ---------------------------------------------------------------------------
# 1. MAC_STARTUP_CLEAR_HOLD=1 → DELETE dispatched, hold_cleared=True in output
# ---------------------------------------------------------------------------

def test_startup_clear_hold_enabled(worker_main, monkeypatch, capsys):
    monkeypatch.setenv("MAC_STARTUP_CLEAR_HOLD", "1")
    monkeypatch.setattr(worker, "register_worker", lambda *_a, **_kw: _fake_register())
    assert worker.main(_register_args()) == 0
    client = worker_main[-1]
    delete_calls = [r for r in client.requests if r[0] == "DELETE"]
    assert any("/dispatch-hold" in r[1] for r in delete_calls), (
        "Expected a DELETE /…/dispatch-hold request when MAC_STARTUP_CLEAR_HOLD=1"
    )
    out = json.loads(capsys.readouterr().out)
    assert out["hold_cleared"] is True


# ---------------------------------------------------------------------------
# 2. MAC_STARTUP_CLEAR_HOLD=0 → DELETE skipped, hold_cleared=False in output
# ---------------------------------------------------------------------------

def test_startup_clear_hold_disabled(worker_main, monkeypatch, capsys):
    monkeypatch.setenv("MAC_STARTUP_CLEAR_HOLD", "0")
    monkeypatch.setattr(worker, "register_worker", lambda *_a, **_kw: _fake_register())
    assert worker.main(_register_args()) == 0
    client = worker_main[-1]
    delete_calls = [r for r in client.requests if r[0] == "DELETE"]
    assert not any("/dispatch-hold" in r[1] for r in delete_calls), (
        "DELETE /…/dispatch-hold must NOT be called when MAC_STARTUP_CLEAR_HOLD=0"
    )
    out = json.loads(capsys.readouterr().out)
    assert out["hold_cleared"] is False


# ---------------------------------------------------------------------------
# 3. MAC_STARTUP_EMIT_CHECKOUT_SHA=1 → checkout_sha present in output
# ---------------------------------------------------------------------------

def test_startup_emit_checkout_sha_enabled(worker_main, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MAC_STARTUP_EMIT_CHECKOUT_SHA", "1")
    monkeypatch.setenv("MAC_STARTUP_CLEAR_HOLD", "0")
    monkeypatch.setattr(worker, "register_worker", lambda *_a, **_kw: _fake_register())
    fake_sha = "a" * 40
    monkeypatch.setattr(
        worker.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (fake_sha + "\n").encode(),
    )
    args = _register_args() + ["--self-update-repo", str(tmp_path)]
    assert worker.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checkout_sha"] == fake_sha


# ---------------------------------------------------------------------------
# 4. MAC_STARTUP_EMIT_CHECKOUT_SHA=0 → checkout_sha omitted (None) in output
# ---------------------------------------------------------------------------

def test_startup_emit_checkout_sha_disabled(worker_main, monkeypatch, capsys):
    monkeypatch.setenv("MAC_STARTUP_EMIT_CHECKOUT_SHA", "0")
    monkeypatch.setenv("MAC_STARTUP_CLEAR_HOLD", "0")
    monkeypatch.setattr(worker, "register_worker", lambda *_a, **_kw: _fake_register())
    assert worker.main(_register_args()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["checkout_sha"] is None


# ---------------------------------------------------------------------------
# 5. MAC_STARTUP_IMPORT_SELF_CHECK=1 → import_self_check result in output
# ---------------------------------------------------------------------------

def test_startup_import_self_check_enabled(worker_main, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MAC_STARTUP_IMPORT_SELF_CHECK", "1")
    monkeypatch.setenv("MAC_STARTUP_CLEAR_HOLD", "0")
    monkeypatch.setenv("MAC_STARTUP_EMIT_CHECKOUT_SHA", "0")
    monkeypatch.setattr(worker, "register_worker", lambda *_a, **_kw: _fake_register())
    monkeypatch.setattr(worker, "_startup_import_self_check", lambda _repo: "ok")
    args = _register_args() + ["--self-update-repo", str(tmp_path)]
    assert worker.main(args) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["import_self_check"] == "ok"


# ---------------------------------------------------------------------------
# 6. MAC_STARTUP_IMPORT_SELF_CHECK=0 → import_self_check skipped (None) in output
# ---------------------------------------------------------------------------

def test_startup_import_self_check_disabled(worker_main, monkeypatch, capsys):
    monkeypatch.setenv("MAC_STARTUP_IMPORT_SELF_CHECK", "0")
    monkeypatch.setenv("MAC_STARTUP_CLEAR_HOLD", "0")
    monkeypatch.setenv("MAC_STARTUP_EMIT_CHECKOUT_SHA", "0")
    monkeypatch.setattr(worker, "register_worker", lambda *_a, **_kw: _fake_register())
    assert worker.main(_register_args()) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["import_self_check"] is None
