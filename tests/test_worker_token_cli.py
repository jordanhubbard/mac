"""`mac admin worker-token issue|rotate|list` over the worker_credentials lifecycle.

`--install` is exercised only with a fake ssh runner and a local env file; it is
never pointed at a real host.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac import worker_token_cli
from mac.cli import main
from mac.deploy_env import read_env_file
from mac.services import ControlPlane
from mac.test_support import dsn_for, ephemeral_dsn, store_on
from mac.worker_credentials import WorkerCredentialPrincipalProvider, _token_hash


@pytest.fixture
def db() -> str:
    dsn = ephemeral_dsn()
    cp = ControlPlane(
        store_on(dsn, initialize=True), secret_key="worker-token-test-key-32-bytes-long"
    )
    machine = cp.register_machine("natasha", machine_id="machine_natasha", labels={}, resources={})
    cp.register_agent(machine.id, "natasha", ["python"], resources={}, agent_id="agent_natasha")
    return dsn_for(dsn)


def _cli(db: str, *argv: str) -> int:
    return main(["--db", db, "admin", "worker-token", *argv])


def _rows(db: str) -> list[dict]:
    store = store_on(db)
    return [
        dict(r)
        for r in store.query_all("SELECT * FROM worker_credentials ORDER BY credential_version")
    ]


def _issue(db: str, capsys, *extra: str) -> str:
    assert _cli(db, "issue", "agent_natasha", *extra) == 0
    out, err = capsys.readouterr()
    token = out.strip()
    assert token.startswith("mac_worker_") and "\n" not in token
    assert token not in err
    return token


def test_issue_prints_token_once_and_makes_it_the_only_active_credential(db, capsys) -> None:
    token = _issue(db, capsys)
    (row,) = _rows(db)
    assert row["state"] == "active" and row["activated_at"]
    assert row["token_hash"] == _token_hash(token)
    assert token not in json.dumps(row, default=str)
    expires = datetime.fromisoformat(str(row["expires_at"]).replace("Z", "+00:00"))
    assert abs(expires - (datetime.now(timezone.utc) + timedelta(days=365))) < timedelta(minutes=5)
    projected = WorkerCredentialPrincipalProvider(store_on(db)).tokens()
    assert projected[_token_hash(token)]["agent_id"] == "agent_natasha"
    assert projected[_token_hash(token)]["worker_credential_state"] == "active"


def test_rotate_supersedes_the_previous_token(db, capsys) -> None:
    first = _issue(db, capsys)
    assert _cli(db, "rotate", "agent_natasha", "--days", "7") == 0
    second = capsys.readouterr().out.strip()
    assert second != first
    old, new = _rows(db)
    assert old["state"] == "superseded" and old["superseded_by"] == new["id"]
    assert new["state"] == "active" and new["credential_version"] == 2
    expires = datetime.fromisoformat(str(new["expires_at"]).replace("Z", "+00:00"))
    assert expires - datetime.now(timezone.utc) < timedelta(days=8)
    projected = WorkerCredentialPrincipalProvider(store_on(db)).tokens()
    assert _token_hash(second) in projected and _token_hash(first) not in projected


def test_out_writes_a_private_file_and_keeps_the_token_off_stdout(db, capsys, tmp_path) -> None:
    out_file = tmp_path / "token"
    assert _cli(db, "issue", "agent_natasha", "--out", str(out_file)) == 0
    stdout = capsys.readouterr().out
    token = out_file.read_text().strip()
    assert token.startswith("mac_worker_") and token not in stdout
    assert out_file.stat().st_mode & 0o777 == 0o600
    assert json.loads(stdout)["token_written_to"] == str(out_file)


def test_list_never_shows_token_or_hash(db, capsys) -> None:
    token = _issue(db, capsys)
    assert _cli(db, "list", "agent_natasha") == 0
    out = capsys.readouterr().out
    (row,) = json.loads(out)
    assert row["state"] == "active" and row["agent_id"] == "agent_natasha"
    assert "token_hash" not in row and token not in out and "sha256:" not in out
    assert _cli(db, "list") == 0
    assert len(json.loads(capsys.readouterr().out)) == 1


def test_unknown_agent_and_bad_days_are_refused(db, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        _cli(db, "issue", "agent_nobody")
    assert exc.value.code == 1
    assert "requires a registered agent" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        _cli(db, "issue", "agent_natasha", "--days", "0")
    assert _rows(db) == []


def test_install_activates_only_after_the_host_has_the_token(db, capsys, monkeypatch) -> None:
    first = _issue(db, capsys)
    seen = {}

    def fake_install(host: str, token: str) -> None:
        # While installing, the new credential is pending and the old one is
        # still active, so the worker never loses authentication.
        states = {r["credential_version"]: r["state"] for r in _rows(db)}
        seen.update(host=host, token=token, states=states)

    monkeypatch.setattr(worker_token_cli, "install_on_host", fake_install)
    assert _cli(db, "rotate", "agent_natasha", "--install", "jkh@natasha") == 0
    out = capsys.readouterr().out
    assert seen["host"] == "jkh@natasha" and seen["states"] == {1: "active", 2: "pending_install"}
    assert seen["token"] not in out and first not in out
    assert json.loads(out)["installed_on"] == "jkh@natasha"
    assert [r["state"] for r in _rows(db)] == ["superseded", "active"]


def test_failed_install_revokes_the_new_token_and_keeps_the_old_one(
    db, capsys, monkeypatch
) -> None:
    first = _issue(db, capsys)

    def failing_install(host: str, token: str) -> None:
        raise worker_token_cli.InstallError("ssh exit 255", token_installed=False)

    monkeypatch.setattr(worker_token_cli, "install_on_host", failing_install)
    with pytest.raises(SystemExit):
        _cli(db, "rotate", "agent_natasha", "--install", "natasha")
    assert "previous one is still active" in capsys.readouterr().err
    assert [r["state"] for r in _rows(db)] == ["active", "revoked"]
    assert _token_hash(first) in WorkerCredentialPrincipalProvider(store_on(db)).tokens()


def test_install_sends_the_token_on_stdin_never_in_argv() -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0)

    token = "mac_worker_" + "s3cret" * 6
    worker_token_cli.install_on_host("jkh@100.87.229.125", token, run=fake_run)
    ((argv, kwargs),) = calls
    assert argv[:1] == ["ssh"] and argv[-3:] == ["jkh@100.87.229.125", "bash", "-s"]
    assert token not in " ".join(argv)
    script = kwargs["input"]
    assert "printf '%s' " + token in script
    assert "sudo -n systemctl restart mac-agent" in script
    assert 'mac.hermes_chat_config --hermes-home "$HOME/.hermes" --mac-env "$env_file"' in script


def _run_env_update(env_file: Path, token: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", worker_token_cli.REMOTE_ENV_UPDATE, str(env_file)],
        input=token,
        capture_output=True,
        text=True,
        check=False,
    )


def test_remote_env_update_rewrites_whichever_token_keys_the_host_uses(tmp_path) -> None:
    env_file = tmp_path / "mac.env"
    env_file.write_text(
        "MAC_HUB_URL=http://hub:8789\nMAC_WORKER_TOKEN__MAC=old\nMAC_WORKER_TOKEN=old\n"
        "MAC_HERMES_GATEWAY_API_KEY=old\nNVIDIA_API_KEY=nvapi-upstream\n"
    )
    new = "mac_worker_" + "n" * 43
    result = _run_env_update(env_file, new)
    assert result.returncode == 0, result.stderr
    assert new not in result.stdout + result.stderr
    values = read_env_file(env_file)
    assert values["MAC_WORKER_TOKEN__MAC"] == values["MAC_WORKER_TOKEN"] == new
    assert values["MAC_HERMES_GATEWAY_API_KEY"] == new
    assert values["NVIDIA_API_KEY"] == "nvapi-upstream"
    assert values["MAC_HUB_URL"] == "http://hub:8789"
    assert env_file.stat().st_mode & 0o777 == 0o600


def test_remote_env_update_adds_the_flat_key_when_none_exists(tmp_path) -> None:
    env_file = tmp_path / "mac.env"
    env_file.write_text("MAC_HUB_URL=http://hub:8789\n")
    assert _run_env_update(env_file, "mac_worker_" + "z" * 43).returncode == 0
    assert read_env_file(env_file)["MAC_WORKER_TOKEN"] == "mac_worker_" + "z" * 43
    refused = _run_env_update(env_file, "not-a-token")
    assert refused.returncode != 0 and "refusing" in refused.stderr


def test_restart_failure_after_the_token_landed_still_activates_it(db, capsys, monkeypatch) -> None:
    _issue(db, capsys)

    def half_install(host: str, token: str) -> None:
        raise worker_token_cli.InstallError("restart failed", token_installed=True)

    monkeypatch.setattr(worker_token_cli, "install_on_host", half_install)
    with pytest.raises(SystemExit):
        _cli(db, "rotate", "agent_natasha", "--install", "natasha")
    assert "restart mac-agent and hermes-gateway on natasha by hand" in capsys.readouterr().err
    assert [r["state"] for r in _rows(db)] == ["superseded", "active"]


@pytest.mark.parametrize(("rc", "installed"), [(255, False), (1, False), (3, True)])
def test_install_exit_status_says_whether_the_token_landed(rc, installed) -> None:
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, rc)

    with pytest.raises(worker_token_cli.InstallError) as exc:
        worker_token_cli.install_on_host("natasha", "mac_worker_x", run=fake_run)
    assert exc.value.token_installed is installed


def test_remote_install_script_reports_post_env_failures_as_exit_3(tmp_path) -> None:
    home = tmp_path / "home"
    (home / ".mac" / "venv" / "bin").mkdir(parents=True)
    (home / ".hermes").mkdir()
    (home / ".mac" / "mac.env").write_text("MAC_WORKER_TOKEN=old\n")
    py = home / ".mac" / "venv" / "bin" / "python"
    # env update succeeds (runs the real snippet); hermes_chat_config fails
    py.write_text('#!/bin/sh\n[ "$1" = -m ] && exit 7\nexec "%s" "$@"\n' % sys.executable)
    py.chmod(0o755)
    token = "mac_worker_" + "q" * 43
    result = subprocess.run(
        ["bash", "-s"],
        input=worker_token_cli.remote_install_script(token),
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == worker_token_cli.INSTALLED_BUT_RESTART_FAILED, result.stderr
    assert read_env_file(home / ".mac" / "mac.env")["MAC_WORKER_TOKEN"] == token
    assert token not in result.stdout + result.stderr
