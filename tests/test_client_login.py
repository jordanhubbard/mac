from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import client_login
from mac.client_principals import MANIFEST_SCHEMA
from mac.client_profiles import install_enrollment_manifest, load_profile
from mac.fleet_ssh import FleetSshSpec


class FakeProcess:
    def __init__(self, pid: int = 4321, returncode=None) -> None:
        self.pid = pid
        self.returncode = returncode
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        if self.returncode is None:
            raise subprocess.TimeoutExpired("ssh", timeout)
        return self.returncode


@pytest.fixture()
def login_files(tmp_path, monkeypatch):
    home = tmp_path / ".mac"
    monkeypatch.setenv("MAC_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    identity = tmp_path / "id_login"
    identity.write_text("private key fixture", encoding="utf-8")
    identity.chmod(0o600)
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("hub.example ssh-ed25519 AAAAfixture\n", encoding="utf-8")
    return home, identity, known_hosts


def _spec(identity: Path, known_hosts: Path, **overrides) -> FleetSshSpec:
    values = {
        "fleet": "production",
        "fleet_name": "production",
        "agent": "hub",
        "target": "mac@hub.example",
        "port": 2222,
        "proxy_jump": None,
        "identity_file": str(identity),
        "identity_ref": None,
        "known_hosts_file": str(known_hosts),
        "host_key_policy": "strict",
        "host_key_fingerprint": None,
        "host_ca": None,
        "supervisor": "client-login",
        "os_kind": "linux",
        "control_port": 8789,
    }
    values.update(overrides)
    return FleetSshSpec(**values)


def _manifest(token="mac_client_secure_fixture_token_123456789", version=1):
    return {
        "schema": MANIFEST_SCHEMA,
        "client_id": "laptop",
        "display_name": "Laptop",
        "profile": "production",
        "fleet": "production",
        "connection": {"api_url": "http://127.0.0.1:8789"},
        "ssh": {},
        "credential": {
            "id": "laptop.v%d" % version,
            "token": token,
            "scopes": ["admin", "dispatch", "read", "write"],
            "issued_at": "2026-07-01T00:00:00+00:00",
            "expires_at": "2026-08-01T00:00:00+00:00",
        },
        "capabilities": [],
    }


def _install(login_files, *, token=None):
    _home, identity, known_hosts = login_files
    manifest = client_login._profile_manifest(
        _manifest(token or "mac_client_secure_fixture_token_123456789"),
        _spec(identity, known_hosts),
        profile="production",
        local_port=48789,
        remote_host="127.0.0.1",
        remote_port=8789,
    )
    install_enrollment_manifest(manifest)
    return manifest


def test_default_client_id_and_direct_route_resolution(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    monkeypatch.setattr(client_login.getpass, "getuser", lambda: "Jane User")
    monkeypatch.setattr(client_login.socket, "gethostname", lambda: "Desk.local")
    assert client_login.default_client_id() == "jane-user-desk"

    spec = client_login.resolve_login_spec(
        ssh_target="mac@hub.example:2201",
        fleet="prod",
        agent=None,
        fleets_config=None,
        ssh_port=None,
        proxy_jump="jump@example",
        identity_file=str(identity),
        known_hosts_file=str(known_hosts),
        host_key_fingerprint=None,
        host_ca=None,
        remote_port=None,
    )
    assert spec.target == "mac@hub.example"
    assert spec.port == 2201
    assert spec.control_port == 8789
    assert spec.proxy_jump == "jump@example"
    with pytest.raises(client_login.ClientLoginError, match="select --ssh"):
        client_login.resolve_login_spec(
            ssh_target=None,
            fleet=None,
            agent=None,
            fleets_config=None,
            ssh_port=None,
            proxy_jump=None,
            identity_file=None,
            known_hosts_file=None,
            host_key_fingerprint=None,
            host_ca=None,
            remote_port=None,
        )


def test_fleet_route_resolution_applies_explicit_overrides(login_files, tmp_path):
    _home, identity, known_hosts = login_files
    fleets = tmp_path / "fleets.yaml"
    fleets.write_text(
        "fleets:\n  prod:\n    hub_agent: hub\n    control_port: 9999\n"
        "    agents:\n      - name: hub\n        target: old@hub\n",
        encoding="utf-8",
    )
    spec = client_login.resolve_login_spec(
        ssh_target=None,
        fleet="prod",
        agent=None,
        fleets_config=str(fleets),
        ssh_port=2222,
        proxy_jump="jump@host",
        identity_file=str(identity),
        known_hosts_file=str(known_hosts),
        host_key_fingerprint="SHA256:pin",
        host_ca=None,
        remote_port=None,
    )
    assert spec.port == 2222
    assert spec.control_port == 9999
    assert spec.identity_file == str(identity)
    assert spec.host_key_fingerprint == "SHA256:pin"


def test_prepare_login_spec_pins_explicit_identity_and_trust(login_files):
    _home, identity, known_hosts = login_files
    prepared, created = client_login.prepare_login_spec(_spec(identity, known_hosts), "production")
    assert prepared.identity_file == str(identity.resolve())
    assert prepared.known_hosts_file == str(known_hosts.resolve())
    assert prepared.host_key_policy == "strict"
    assert created is None

    # An explicitly-supplied identity file is still validated strictly.
    identity.chmod(0o644)
    with pytest.raises(client_login.ClientLoginError, match="chmod 600"):
        client_login.prepare_login_spec(_spec(identity, known_hosts), "production")
    identity.chmod(0o600)


def test_prepare_login_spec_defers_to_ssh_defaults_when_unset(login_files):
    _home, identity, known_hosts = login_files

    # No identity file: defer to ssh's default keys / agent, keep explicit trust.
    prepared, created = client_login.prepare_login_spec(
        _spec(identity, known_hosts, identity_file=None), "production"
    )
    assert prepared.identity_file is None
    assert prepared.identity_ref is None
    assert prepared.known_hosts_file == str(known_hosts.resolve())
    assert prepared.host_key_policy == "strict"
    assert created is None

    # No host trust: fall back to the default known_hosts via accept-new (TOFU).
    prepared, created = client_login.prepare_login_spec(
        _spec(identity, known_hosts, known_hosts_file=None), "production"
    )
    assert prepared.identity_file == str(identity.resolve())
    assert prepared.known_hosts_file is None
    assert prepared.host_ca is None
    assert prepared.host_key_fingerprint is None
    assert prepared.host_key_policy == "accept-new"
    assert created is None

    # Nothing explicit at all: behave like `ssh <host>` — no pinned files.
    prepared, created = client_login.prepare_login_spec(
        _spec(identity, known_hosts, identity_file=None, known_hosts_file=None),
        "production",
    )
    assert prepared.identity_file is None
    assert prepared.known_hosts_file is None
    assert prepared.host_key_policy == "accept-new"
    assert created is None

    # An explicit `insecure` policy is preserved even without trust material.
    prepared, _created = client_login.prepare_login_spec(
        _spec(
            identity,
            known_hosts,
            known_hosts_file=None,
            host_key_policy="insecure",
        ),
        "production",
    )
    assert prepared.host_key_policy == "insecure"


def test_prepare_login_spec_materializes_matching_fingerprint(login_files, monkeypatch):
    home, identity, known_hosts = login_files
    fingerprint = "SHA256:fixturePin="

    def fake_run(argv, **kwargs):
        if argv[0] == "ssh-keyscan":
            return SimpleNamespace(
                returncode=0,
                stdout="hub.example ssh-ed25519 AAAAfixture\n",
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout="256 %s fixture (ED25519)\n" % fingerprint,
            stderr="",
        )

    monkeypatch.setattr(client_login.subprocess, "run", fake_run)
    prepared, created = client_login.prepare_login_spec(
        _spec(
            identity,
            known_hosts,
            known_hosts_file=None,
            host_key_fingerprint=fingerprint,
        ),
        "production",
    )
    assert created == home / "ssh" / "production.known_hosts"
    assert prepared.known_hosts_file == str(created)
    assert created.stat().st_mode & 0o777 == 0o600

    with pytest.raises(client_login.ClientLoginError, match="ProxyJump"):
        client_login.prepare_login_spec(
            _spec(
                identity,
                known_hosts,
                known_hosts_file=None,
                host_key_fingerprint=fingerprint,
                proxy_jump="jump@host",
            ),
            "other",
        )


def test_prepare_login_spec_rejects_fingerprint_mismatch(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    monkeypatch.setattr(client_login, "_existing_fingerprints", lambda _path: {"SHA256:other"})
    with pytest.raises(client_login.ClientLoginError, match="does not contain"):
        client_login.prepare_login_spec(
            _spec(identity, known_hosts, host_key_fingerprint="SHA256:wanted"),
            "production",
        )


def test_resolve_remote_mac_discovers_and_tolerates_absence(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    spec = _spec(identity, known_hosts)

    # A hit is returned from the sentinel-tagged probe output, banner and all.
    def found(argv, **_kwargs):
        assert _spec_probe_target(argv)
        return SimpleNamespace(
            returncode=0,
            stdout="Welcome to the hub!\nMAC_BIN=/Users/jkh/.local/bin/mac\n",
            stderr="",
        )

    monkeypatch.setattr(client_login.subprocess, "run", found)
    assert client_login._resolve_remote_mac(spec, timeout=5) == "/Users/jkh/.local/bin/mac"

    # No sentinel (not found) -> None, so the caller falls back to bare `mac`.
    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=3, stdout="", stderr=""),
    )
    assert client_login._resolve_remote_mac(spec, timeout=5) is None

    # A transport failure is swallowed rather than aborting login.
    def boom(*_a, **_k):
        raise OSError("ssh unavailable")

    monkeypatch.setattr(client_login.subprocess, "run", boom)
    assert client_login._resolve_remote_mac(spec, timeout=5) is None


def _spec_probe_target(argv):
    return argv[0] == "ssh" and argv[-2] == "mac@hub.example"


def test_choose_local_port_and_tunnel_argv(login_files):
    _home, identity, known_hosts = login_files
    chosen = client_login.choose_local_port()
    assert 0 < chosen <= 65535
    with pytest.raises(client_login.ClientLoginError, match="between"):
        client_login.choose_local_port(70000)
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        port = occupied.getsockname()[1]
        with pytest.raises(client_login.ClientLoginError, match="already in use"):
            client_login.choose_local_port(port)
    argv = client_login._tunnel_argv(_spec(identity, known_hosts), 49000, "127.0.0.1", 8789, 7)
    assert argv[0] == "ssh"
    assert "ExitOnForwardFailure=yes" in argv
    assert "127.0.0.1:49000:127.0.0.1:8789" in argv


def test_start_tunnel_success_exit_and_timeout(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    process = FakeProcess()
    monkeypatch.setattr(client_login.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: True)
    assert (
        client_login._start_tunnel(
            _spec(identity, known_hosts), 49000, "127.0.0.1", 8789, timeout=1
        )
        is process
    )

    exited = FakeProcess(returncode=255)
    monkeypatch.setattr(client_login.subprocess, "Popen", lambda *_a, **_k: exited)
    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: False)
    with pytest.raises(client_login.ClientLoginError, match="exited"):
        client_login._start_tunnel(
            _spec(identity, known_hosts), 49000, "127.0.0.1", 8789, timeout=1
        )

    timeout_process = FakeProcess()
    monkeypatch.setattr(client_login.subprocess, "Popen", lambda *_a, **_k: timeout_process)
    ticks = iter((0.0, 2.0, 2.0))
    monkeypatch.setattr(client_login.time, "monotonic", lambda: next(ticks))
    with pytest.raises(client_login.ClientLoginError, match="did not become ready"):
        client_login._start_tunnel(
            _spec(identity, known_hosts), 49000, "127.0.0.1", 8789, timeout=1
        )
    assert timeout_process.terminated


def test_login_validates_before_atomic_install_and_never_returns_token(login_files, monkeypatch):
    home, identity, known_hosts = login_files
    process = FakeProcess()
    commands = []
    monkeypatch.setattr(client_login, "choose_local_port", lambda _port=None: 48789)
    monkeypatch.setattr(client_login, "_start_tunnel", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_resolve_remote_mac", lambda *_a, **_k: None)
    monkeypatch.setattr(
        client_login,
        "_run_remote_json",
        lambda _spec, command, **_k: commands.append(command) or _manifest(),
    )
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (True, "authenticated"))

    result = client_login.login(
        spec=_spec(identity, known_hosts),
        profile="production",
        client_id="laptop",
        display_name="Laptop",
        local_port=48789,
    )

    stored = load_profile("production", include_token=True)
    state = json.loads((home / "sessions" / "production.json").read_text())
    assert result["status"] == "logged_in"
    assert stored["credential"]["token"] == _manifest()["credential"]["token"]
    assert state["ssh_pid"] == process.pid
    assert "token" not in json.dumps(result).lower()
    assert _manifest()["credential"]["token"] not in json.dumps(state)
    assert "--scopes" in commands[0]


def test_login_rolls_back_and_revokes_after_malformed_manifest(login_files, monkeypatch):
    home, identity, known_hosts = login_files
    process = FakeProcess()
    actions = []
    monkeypatch.setattr(client_login, "choose_local_port", lambda _port=None: 48789)
    monkeypatch.setattr(client_login, "_start_tunnel", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_resolve_remote_mac", lambda *_a, **_k: None)
    monkeypatch.setattr(
        client_login,
        "_run_remote_json",
        lambda *_a, **_k: {"schema": MANIFEST_SCHEMA, "credential": {}},
    )
    monkeypatch.setattr(
        client_login,
        "_run_remote_action",
        lambda _spec, command, **_k: actions.append(command),
    )

    with pytest.raises(client_login.ClientLoginError) as caught:
        client_login.login(
            spec=_spec(identity, known_hosts),
            profile="production",
            client_id="laptop",
            local_port=48789,
        )
    assert process.terminated
    assert actions and "revoke" in actions[0]
    assert not (home / "clients" / "production.yaml").exists()
    assert not (home / "sessions" / "production.json").exists()
    assert "mac_client_" not in str(caught.value)


def test_login_rejects_duplicate_without_rotating(login_files):
    _install(login_files)
    _home, identity, known_hosts = login_files
    with pytest.raises(client_login.ClientLoginError, match="already exists"):
        client_login.login(
            spec=_spec(identity, known_hosts),
            profile="production",
            client_id="laptop",
        )


def test_rotating_login_stops_previous_managed_tunnel(login_files, monkeypatch):
    _install(login_files)
    _home, identity, known_hosts = login_files
    previous = client_login._write_state(
        "production", {"ssh_pid": 111, "ssh_target": "mac@hub.example"}
    )
    stopped = []
    process = FakeProcess(222)
    monkeypatch.setattr(
        client_login,
        "_stop_managed_state",
        lambda state: stopped.append(state) or True,
    )
    monkeypatch.setattr(client_login, "choose_local_port", lambda *_a: 48790)
    monkeypatch.setattr(client_login, "_start_tunnel", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_resolve_remote_mac", lambda *_a, **_k: None)
    monkeypatch.setattr(
        client_login,
        "_run_remote_json",
        lambda *_a, **_k: _manifest("mac_client_rotated_login_token_123456789", version=2),
    )
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (True, "authenticated"))
    result = client_login.login(
        spec=_spec(identity, known_hosts),
        profile="production",
        client_id="laptop",
        rotate=True,
        local_port=48790,
    )
    assert result["status"] == "logged_in"
    assert stopped == [previous]


def test_login_cleans_interrupted_session_without_profile(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    previous = client_login._write_state(
        "production", {"ssh_pid": 111, "ssh_target": "mac@hub.example"}
    )
    stopped = []
    process = FakeProcess(222)
    monkeypatch.setattr(
        client_login,
        "_stop_managed_state",
        lambda state: stopped.append(state) or True,
    )
    monkeypatch.setattr(client_login, "choose_local_port", lambda *_a: 48790)
    monkeypatch.setattr(client_login, "_start_tunnel", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_resolve_remote_mac", lambda *_a, **_k: None)
    monkeypatch.setattr(client_login, "_run_remote_json", lambda *_a, **_k: _manifest())
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (True, "authenticated"))
    client_login.login(
        spec=_spec(identity, known_hosts),
        profile="production",
        client_id="laptop",
        local_port=48790,
    )
    assert stopped == [previous]


def test_ensure_session_running_reconnect_conflict_and_auth_failure(login_files, monkeypatch):
    _install(login_files)
    _home, identity, known_hosts = login_files
    state = client_login._write_state(
        "production",
        {
            "ssh_pid": 222,
            "ssh_target": "mac@hub.example",
            "local_port": 48789,
        },
    )
    monkeypatch.setattr(client_login, "_managed_process", lambda value: value == state)
    monkeypatch.setattr(client_login, "_port_open", lambda port, **_k: port == 48789)
    assert client_login.ensure_session("production")["status"] == "running"

    client_login._remove_state("production")
    process = FakeProcess(333)
    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: False)
    monkeypatch.setattr(client_login, "prepare_login_spec", lambda spec, *_a, **_k: (spec, None))
    monkeypatch.setattr(client_login, "_start_tunnel", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_resolve_remote_mac", lambda *_a, **_k: None)
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (True, "authenticated"))
    assert client_login.ensure_session("production")["status"] == "reconnected"

    client_login._remove_state("production")
    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: True)
    with pytest.raises(client_login.ClientLoginError, match="unmanaged"):
        client_login.ensure_session("production")

    client_login._remove_state("production")
    failing = FakeProcess(444)
    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: False)
    monkeypatch.setattr(client_login, "_start_tunnel", lambda *_a, **_k: failing)
    monkeypatch.setattr(
        client_login,
        "_validate_token",
        lambda *_a, **_k: (False, "credential_rejected"),
    )
    with pytest.raises(client_login.ClientLoginError, match="failed authentication"):
        client_login.ensure_session("production")
    assert failing.terminated


def test_login_status_is_secret_free_for_connected_and_stopped(login_files, monkeypatch):
    manifest = _install(login_files)
    client_login._write_state(
        "production",
        {
            "ssh_pid": 222,
            "ssh_target": "mac@hub.example",
            "local_port": 48789,
        },
    )
    monkeypatch.setattr(client_login, "_managed_process", lambda _state: True)
    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: True)
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (True, "authenticated"))
    result = client_login.login_status("production")
    assert result["status"] == "connected"
    assert result["authenticated"] is True
    assert manifest["credential"]["token"] not in json.dumps(result)

    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: False)
    result = client_login.login_status("production")
    assert result["status"] == "stopped"
    assert result["authenticated"] is False


def test_renew_rotates_only_after_validation(login_files, monkeypatch):
    first = _install(login_files)
    second = _manifest("mac_client_rotated_fixture_token_987654321", version=2)
    actions = []
    monkeypatch.setattr(client_login, "_ensure_session_unlocked", lambda *_a: {"status": "running"})
    monkeypatch.setattr(client_login, "prepare_login_spec", lambda spec, *_a, **_k: (spec, None))
    monkeypatch.setattr(client_login, "_run_remote_json", lambda *_a, **_k: second)
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (True, "authenticated"))
    result = client_login.renew_login("production")
    assert result["status"] == "renewed"
    stored = load_profile("production", include_token=True)
    assert stored["credential"]["token"] == second["credential"]["token"]

    third = _manifest("mac_client_rejected_fixture_token_987654321", version=3)
    monkeypatch.setattr(client_login, "_run_remote_json", lambda *_a, **_k: third)
    monkeypatch.setattr(
        client_login,
        "_validate_token",
        lambda *_a, **_k: (False, "credential_rejected"),
    )
    monkeypatch.setattr(
        client_login,
        "_run_remote_action",
        lambda _spec, command, **_k: actions.append(command),
    )
    with pytest.raises(client_login.ClientLoginError, match="failed validation"):
        client_login.renew_login("production")
    assert actions and "revoke" in actions[0]
    assert first["credential"]["token"] != second["credential"]["token"]
    stored = load_profile("production", include_token=True)
    assert stored["credential"]["token"] == second["credential"]["token"]


def test_logout_revokes_before_removing_profile_and_managed_pin(login_files, monkeypatch):
    home, identity, _known_hosts = login_files
    managed = client_login.managed_known_hosts_path("production")
    managed.parent.mkdir(parents=True)
    managed.write_text("hub key\n", encoding="utf-8")
    managed.chmod(0o600)
    manifest = client_login._profile_manifest(
        _manifest(),
        _spec(identity, managed),
        profile="production",
        local_port=48789,
        remote_host="127.0.0.1",
        remote_port=8789,
    )
    install_enrollment_manifest(manifest)
    client_login._write_state("production", {"ssh_pid": 222, "ssh_target": "mac@hub.example"})
    order = []
    monkeypatch.setattr(client_login, "prepare_login_spec", lambda spec, *_a, **_k: (spec, None))
    monkeypatch.setattr(
        client_login,
        "_run_remote_action",
        lambda *_a, **_k: order.append("revoke"),
    )
    monkeypatch.setattr(
        client_login,
        "_stop_managed_state",
        lambda *_a, **_k: order.append("stop") or True,
    )
    result = client_login.logout("production", revoke=True)
    assert order == ["revoke", "stop"]
    assert result["revoked"] is True
    assert not (home / "clients" / "production.yaml").exists()
    assert not managed.exists()


def test_state_permissions_schema_and_managed_process_edges(login_files, monkeypatch):
    home, _identity, _known_hosts = login_files
    path = home / "sessions" / "production.json"
    path.parent.mkdir(parents=True)
    path.write_text("{}", encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(client_login.ClientLoginError, match="schema"):
        client_login._read_state("production")
    path.chmod(0o644)
    with pytest.raises(client_login.ClientLoginError, match="permissions"):
        client_login._read_state("production")

    state = {"ssh_pid": 123, "ssh_target": "mac@hub.example"}
    monkeypatch.setattr(client_login, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout="ssh -N -L 127.0.0.1:1:127.0.0.1:2 mac@hub.example",
        ),
    )
    assert client_login._managed_process(state) is True
    monkeypatch.setattr(client_login, "_pid_alive", lambda _pid: False)
    assert client_login._managed_process(state) is False


def test_remote_json_and_action_fail_closed(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    spec = _spec(identity, known_hosts)
    secret = _manifest()["credential"]["token"]
    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout=json.dumps(_manifest())),
    )
    result = client_login._run_remote_json(spec, ["mac", "enroll"], timeout=1)
    assert result["client_id"] == "laptop"
    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=7, stdout=secret, stderr=secret),
    )
    with pytest.raises(client_login.ClientLoginError) as caught:
        client_login._run_remote_json(spec, ["mac", "enroll"], timeout=1)
    assert secret not in str(caught.value)
    with pytest.raises(client_login.ClientLoginError, match="revocation"):
        client_login._run_remote_action(spec, ["mac", "revoke"], timeout=1)


def test_validate_token_maps_success_http_and_network_failures(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return b'{"open": 1}'

    monkeypatch.setattr(client_login.urllib.request, "urlopen", lambda *_a, **_k: Response())
    assert client_login._validate_token("http://127.0.0.1:1", "token", timeout=1) == (
        True,
        "authenticated",
    )

    def forbidden(*_args, **_kwargs):
        raise urllib.error.HTTPError("url", 403, "forbidden", {}, None)

    monkeypatch.setattr(client_login.urllib.request, "urlopen", forbidden)
    assert client_login._validate_token("http://127.0.0.1:1", "token", timeout=1) == (
        False,
        "credential_rejected",
    )
    monkeypatch.setattr(
        client_login.urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError("down")),
    )
    assert client_login._validate_token("http://127.0.0.1:1", "token", timeout=1) == (
        False,
        "hub_unreachable",
    )


def test_private_file_fingerprint_and_target_helper_edges(login_files, monkeypatch, tmp_path):
    home, identity, _known_hosts = login_files
    assert client_login._read_state("missing") == {}
    bad_state = home / "sessions" / "bad.json"
    bad_state.parent.mkdir(parents=True)
    bad_state.write_text("{", encoding="utf-8")
    bad_state.chmod(0o600)
    with pytest.raises(client_login.ClientLoginError, match="could not read"):
        client_login._read_state("bad")
    with pytest.raises(client_login.ClientLoginError, match="explicit SSH identity"):
        client_login._private_identity(None)
    with pytest.raises(client_login.ClientLoginError, match="does not exist"):
        client_login._private_identity(str(tmp_path / "missing-key"))

    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout="256 SHA256:first host\n256 SHA256:second host\n",
        ),
    )
    assert client_login._existing_fingerprints(identity) == {
        "SHA256:first",
        "SHA256:second",
    }
    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(client_login.ClientLoginError, match="could not inspect"):
        client_login._existing_fingerprints(identity)
    assert client_login._target_host("user@[2001:db8::1]") == "2001:db8::1"


def test_host_pin_and_route_error_edges(login_files, monkeypatch, tmp_path):
    _home, identity, known_hosts = login_files
    fingerprint = "SHA256:wanted"

    def no_matching_key(argv, **_kwargs):
        if argv[0] == "ssh-keyscan":
            return SimpleNamespace(
                returncode=0,
                stdout="# banner\n\nhub ssh-ed25519 AAAAother\n",
            )
        return SimpleNamespace(returncode=0, stdout="256 SHA256:other key\n")

    monkeypatch.setattr(client_login.subprocess, "run", no_matching_key)
    with pytest.raises(client_login.ClientLoginError, match="does not match"):
        client_login._pin_scanned_fingerprint(
            _spec(identity, known_hosts, known_hosts_file=None),
            "production",
            fingerprint,
            timeout=1,
        )

    with pytest.raises(client_login.ClientLoginError, match="does not exist"):
        client_login.prepare_login_spec(_spec(identity, tmp_path / "absent"), "production")
    host_ca = tmp_path / "host_ca"
    host_ca.write_text("@cert-authority *.example fixture\n", encoding="utf-8")
    prepared, _ = client_login.prepare_login_spec(
        _spec(identity, known_hosts, known_hosts_file=None, host_ca=str(host_ca)),
        "production",
    )
    assert prepared.host_ca == str(host_ca.resolve())

    with pytest.raises(client_login.ClientLoginError, match="positive"):
        client_login.resolve_login_spec(
            ssh_target="mac@hub",
            fleet=None,
            agent=None,
            fleets_config=None,
            ssh_port=0,
            proxy_jump=None,
            identity_file=str(identity),
            known_hosts_file=str(known_hosts),
            host_key_fingerprint=None,
            host_ca=None,
            remote_port=None,
        )
    with pytest.raises(client_login.ClientLoginError, match="fleets config"):
        client_login.resolve_login_spec(
            ssh_target=None,
            fleet="missing",
            agent=None,
            fleets_config=str(tmp_path / "no-fleets.yaml"),
            ssh_port=None,
            proxy_jump=None,
            identity_file=None,
            known_hosts_file=None,
            host_key_fingerprint=None,
            host_ca=None,
            remote_port=None,
        )


def test_port_process_and_stop_helpers(login_files, monkeypatch):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert client_login._port_open(port) is True
    assert client_login._port_open(port) is False

    assert client_login._pid_alive(os.getpid()) is True
    assert client_login._pid_alive("not-a-pid") is False
    assert client_login._managed_process({}) is False
    assert client_login._stop_managed_state({}) is False

    alive = iter((True, False, False))
    signals = []
    monkeypatch.setattr(client_login, "_managed_process", lambda _state: True)
    monkeypatch.setattr(client_login, "_pid_alive", lambda _pid: next(alive))
    monkeypatch.setattr(client_login.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(client_login.time, "sleep", lambda _delay: None)
    assert client_login._stop_managed_state({"ssh_pid": 123}) is True
    assert signals == [(123, signal.SIGTERM)]


def test_terminate_process_kills_stubborn_child():
    class Stubborn(FakeProcess):
        def terminate(self):
            self.terminated = True

    process = Stubborn()
    client_login._terminate_process(process)
    assert process.terminated and process.killed
    client_login._terminate_process(None)
    client_login._terminate_process(FakeProcess(returncode=0))


def test_start_tunnel_waits_for_second_probe(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    process = FakeProcess()
    probes = iter((False, True))
    monkeypatch.setattr(client_login.subprocess, "Popen", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: next(probes))
    monkeypatch.setattr(client_login.time, "sleep", lambda _delay: None)
    assert (
        client_login._start_tunnel(
            _spec(identity, known_hosts), 49000, "127.0.0.1", 8789, timeout=1
        )
        is process
    )


def test_remote_json_rejects_malformed_and_non_object(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    spec = _spec(identity, known_hosts)
    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="{"),
    )
    with pytest.raises(client_login.ClientLoginError, match="malformed"):
        client_login._run_remote_json(spec, ["mac"], timeout=1)
    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="[]"),
    )
    with pytest.raises(client_login.ClientLoginError, match="malformed"):
        client_login._run_remote_json(spec, ["mac"], timeout=1)


def test_missing_or_timed_out_ssh_tools_fail_safely(login_files, monkeypatch):
    _home, identity, known_hosts = login_files
    spec = _spec(identity, known_hosts)
    monkeypatch.setattr(
        client_login.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("missing")),
    )
    with pytest.raises(client_login.ClientLoginError, match="ssh-keygen"):
        client_login._existing_fingerprints(known_hosts)
    with pytest.raises(client_login.ClientLoginError, match="enrollment"):
        client_login._run_remote_json(spec, ["mac"], timeout=1)
    with pytest.raises(client_login.ClientLoginError, match="revocation"):
        client_login._run_remote_action(spec, ["mac"], timeout=1)
    assert client_login._managed_process({"ssh_pid": os.getpid(), "ssh_target": "target"}) is False

    monkeypatch.setattr(
        client_login.subprocess,
        "Popen",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("missing")),
    )
    with pytest.raises(client_login.ClientLoginError, match="OpenSSH"):
        client_login._start_tunnel(spec, 49000, "127.0.0.1", 8789, timeout=1)


def test_stop_managed_state_handles_process_race(monkeypatch):
    monkeypatch.setattr(client_login, "_managed_process", lambda _state: True)
    monkeypatch.setattr(
        client_login.os,
        "kill",
        lambda *_a: (_ for _ in ()).throw(ProcessLookupError()),
    )
    assert client_login._stop_managed_state({"ssh_pid": 123}) is False


def test_login_option_flags_and_validation_rollback(login_files, monkeypatch):
    home, identity, known_hosts = login_files
    process = FakeProcess()
    command_seen = []
    pin = client_login.managed_known_hosts_path("production")
    pin.parent.mkdir(parents=True)
    pin.write_text("pin\n", encoding="utf-8")
    pin.chmod(0o600)
    spec = _spec(identity, known_hosts)
    monkeypatch.setattr(client_login, "prepare_login_spec", lambda *_a, **_k: (spec, pin))
    monkeypatch.setattr(client_login, "choose_local_port", lambda *_a: 48789)
    monkeypatch.setattr(client_login, "_start_tunnel", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_resolve_remote_mac", lambda *_a, **_k: None)
    monkeypatch.setattr(
        client_login,
        "_run_remote_json",
        lambda _spec, command, **_k: command_seen.append(command) or _manifest(),
    )
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (False, "rejected"))
    monkeypatch.setattr(
        client_login,
        "_run_remote_action",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("revoke down")),
    )
    with pytest.raises(client_login.ClientLoginError, match="rejected"):
        client_login.login(
            spec=spec,
            profile="production",
            client_id="laptop",
            capabilities=("tasks",),
            allow_elevated=True,
            rotate=True,
            local_port=48789,
        )
    command = command_seen[0]
    assert "--capabilities" in command
    assert "--allow-elevated" in command
    assert "--rotate" in command
    assert pin.read_text(encoding="utf-8") == "pin\n"

    monkeypatch.setattr(
        client_login,
        "prepare_login_spec",
        lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("unexpected")),
    )
    with pytest.raises(client_login.ClientLoginError, match="before credentials"):
        client_login.login(spec=spec, profile="other", client_id="other")


def test_direct_and_stale_session_recovery_edges(login_files, monkeypatch):
    direct = _manifest()
    direct["connection"] = {
        "api_url": "https://hub.example",
        "mode": "direct",
    }
    direct["ssh"] = {}
    install_enrollment_manifest(direct, profile_override="direct", activate=False)
    assert client_login.ensure_session("direct")["status"] == "direct"

    _install(login_files)
    client_login._write_state(
        "production",
        {"ssh_pid": 555, "ssh_target": "mac@hub.example", "local_port": 48789},
    )
    stopped = []
    process = FakeProcess(556)
    monkeypatch.setattr(
        client_login,
        "_stop_managed_state",
        lambda state: stopped.append(state) or False,
    )
    monkeypatch.setattr(client_login, "_managed_process", lambda _state: False)
    monkeypatch.setattr(client_login, "_port_open", lambda *_a, **_k: False)
    monkeypatch.setattr(client_login, "prepare_login_spec", lambda spec, *_a, **_k: (spec, None))
    monkeypatch.setattr(client_login, "_start_tunnel", lambda *_a, **_k: process)
    monkeypatch.setattr(client_login, "_resolve_remote_mac", lambda *_a, **_k: None)
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (True, "authenticated"))
    assert client_login.ensure_session("production")["status"] == "reconnected"
    assert stopped


def test_no_active_profile_errors_and_direct_status(login_files, monkeypatch):
    monkeypatch.setattr(client_login, "active_profile_name", lambda: None)
    with pytest.raises(client_login.ClientLoginError, match="no active"):
        client_login.login_status()
    with pytest.raises(client_login.ClientLoginError, match="no active"):
        client_login.renew_login()
    with pytest.raises(client_login.ClientLoginError, match="no active"):
        client_login.logout()

    direct = _manifest()
    direct["connection"] = {
        "api_url": "https://hub.example",
        "mode": "direct",
    }
    direct["ssh"] = {}
    install_enrollment_manifest(direct, profile_override="direct", activate=False)
    monkeypatch.setattr(client_login, "_validate_token", lambda *_a, **_k: (True, "authenticated"))
    result = client_login.login_status("direct")
    assert result["mode"] == "direct"
    assert result["local_port"] is None
