from __future__ import annotations

import grp
import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mac import client_login, local_console
from mac.api import create_app
from mac.client_principals import (
    ClientPrincipalError,
    ClientPrincipalProvider,
    ClientPrincipalStore,
)
from mac.client_profiles import load_profile
from mac.local_console import (
    LocalConsoleError,
    LocalConsoleService,
    MAX_FRAME_BYTES,
    PeerIdentity,
    _receive_frame,
    default_api_url,
    request_local_console,
)
from mac.services import ControlPlane


@pytest.fixture()
def short_socket_dir():
    path = Path(tempfile.mkdtemp(prefix="mac-lc-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _request(client_id: str = "console-client") -> dict:
    return {
        "action": "enroll",
        "client_id": client_id,
        "display_name": "Console Client",
        "fleet": "local",
        "profile": "local",
        "scopes": ["read", "write", "dispatch"],
        "capabilities": [],
        "expires_in": 3600,
        "api_url": "http://127.0.0.1:8789",
        "allow_elevated": False,
        "rotate": False,
    }


def test_framing_is_bounded_and_accepts_exactly_one_request():
    class Connection:
        def __init__(self, data):
            self.data = data

        def recv(self, _size):
            data, self.data = self.data, b""
            return data

    with pytest.raises(LocalConsoleError, match="only one"):
        _receive_frame(Connection(b"{}\n{}\n"), MAX_FRAME_BYTES)
    with pytest.raises(LocalConsoleError, match="size limit"):
        _receive_frame(Connection(b"x" * (MAX_FRAME_BYTES + 1)), MAX_FRAME_BYTES)


def test_direct_api_url_environment_order_and_port_fallback(monkeypatch):
    monkeypatch.delenv("MAC_API_URL", raising=False)
    monkeypatch.delenv("MAC_URL", raising=False)
    monkeypatch.delenv("MAC_HUB_URL", raising=False)
    monkeypatch.setenv("MAC_PORT", "9911")
    assert default_api_url() == "http://127.0.0.1:9911"
    monkeypatch.setenv("MAC_HUB_URL", "https://hub.example/")
    assert default_api_url() == "https://hub.example"
    monkeypatch.setenv("MAC_URL", "https://preferred.example/")
    assert default_api_url() == "https://preferred.example"
    monkeypatch.setenv("MAC_API_URL", "https://first.example/")
    assert default_api_url() == "https://first.example"


def test_peer_authorization_and_socket_mode(tmp_path, short_socket_dir):
    group_name = grp.getgrgid(os.getgid()).gr_name
    service = LocalConsoleService(
        short_socket_dir / "console.sock",
        ClientPrincipalStore(tmp_path / "principals.json"),
        allowed_group=group_name,
    )
    assert service._authorized(PeerIdentity(uid=os.geteuid(), gid=os.getegid()))
    assert service._authorized(PeerIdentity(uid=0, gid=0))
    assert not service._authorized(PeerIdentity(uid=987654, gid=987654))
    assert (
        service._dispatch(
            PeerIdentity(uid=os.geteuid(), gid=os.getegid()), _request("direct-check")
        )["client_id"]
        == "direct-check"
    )

    service.start()
    try:
        assert service.path.stat().st_mode & 0o777 == 0o660
        assert service.path.parent.stat().st_mode & 0o777 == 0o750
    finally:
        service.stop()


def test_unauthorized_peer_is_rejected_even_for_loopback_api_url(
    tmp_path, short_socket_dir, monkeypatch
):
    registry = tmp_path / "principals.json"
    service = LocalConsoleService(
        short_socket_dir / "console.sock",
        ClientPrincipalStore(registry),
    )
    monkeypatch.setattr(
        local_console,
        "_peer_identity",
        lambda _conn: PeerIdentity(uid=987654, gid=987654),
    )
    service.start()
    try:
        with pytest.raises(LocalConsoleError, match="access denied"):
            request_local_console(_request(), socket_path=str(service.path))
    finally:
        service.stop()
    assert not registry.exists()


def test_non_root_cannot_request_elevated_local_console_scopes(tmp_path):
    service = LocalConsoleService(
        tmp_path / "console.sock",
        ClientPrincipalStore(tmp_path / "principals.json"),
        service_uid=1234,
    )
    request = _request()
    request["scopes"] = ["read", "admin"]
    request["allow_elevated"] = True
    with pytest.raises(PermissionError, match="require root"):
        service._dispatch(PeerIdentity(uid=1234, gid=1234), request)


def test_root_requires_explicit_elevated_acknowledgement(tmp_path):
    service = LocalConsoleService(
        tmp_path / "console.sock",
        ClientPrincipalStore(tmp_path / "principals.json"),
    )
    request = _request()
    request["scopes"] = ["read", "admin"]
    with pytest.raises(PermissionError):
        service._dispatch(PeerIdentity(uid=0, gid=0), request)


def test_elevated_local_console_renew_requires_root_and_acknowledgement(tmp_path):
    service = LocalConsoleService(
        tmp_path / "console.sock",
        ClientPrincipalStore(tmp_path / "principals.json"),
    )
    request = _request("elevated-client")
    request["scopes"] = ["read", "admin"]
    request["allow_elevated"] = True
    service._dispatch(PeerIdentity(uid=0, gid=0), request)
    renew = {
        "action": "renew",
        "client_id": "elevated-client",
        "expires_in": 3600,
        "allow_elevated": True,
    }
    with pytest.raises(ClientPrincipalError, match="not owned"):
        service._dispatch(PeerIdentity(uid=1234, gid=1234), renew)
    renew["allow_elevated"] = False
    with pytest.raises(ClientPrincipalError, match="not authorized"):
        service._dispatch(PeerIdentity(uid=0, gid=0), renew)
    renew["allow_elevated"] = True
    assert (
        service._dispatch(PeerIdentity(uid=0, gid=0), renew)["credential"]["id"]
        == "elevated-client.v2"
    )


def test_local_console_cannot_mutate_another_uid_or_ssh_credential(tmp_path):
    store = ClientPrincipalStore(tmp_path / "principals.json")
    service = LocalConsoleService(tmp_path / "console.sock", store, service_uid=1234)
    first = _request("first-owner")
    service._dispatch(PeerIdentity(uid=1234, gid=1234), first)
    store.enroll("ssh-client", actor="ssh:operator")

    for client_id in ("first-owner", "ssh-client"):
        with pytest.raises(ClientPrincipalError, match="not owned"):
            service._dispatch(
                PeerIdentity(uid=5678, gid=5678),
                {"action": "renew", "client_id": client_id, "expires_in": 3600},
            )
        with pytest.raises(ClientPrincipalError, match="not owned"):
            service._dispatch(
                PeerIdentity(uid=5678, gid=5678),
                {"action": "revoke", "client_id": client_id},
            )

    rotated = _request("first-owner")
    rotated["rotate"] = True
    with pytest.raises(ClientPrincipalError, match="not owned"):
        service._dispatch(PeerIdentity(uid=5678, gid=5678), rotated)


def test_local_console_login_validates_then_installs_direct_profile(
    tmp_path, short_socket_dir, monkeypatch
):
    home = tmp_path / "home"
    registry = tmp_path / "principals.json"
    service = LocalConsoleService(
        short_socket_dir / "console.sock",
        ClientPrincipalStore(registry),
    )
    monkeypatch.setenv("MAC_HOME", str(home))
    checked = {}

    def validate(api_url, token, *, timeout):
        checked.update(api_url=api_url, token=token, timeout=timeout)
        return True, "authenticated"

    monkeypatch.setattr(client_login, "_validate_token", validate)
    service.start()
    try:
        result = client_login.local_console_login(
            profile="local",
            client_id="console-client",
            socket_path=str(service.path),
            api_url="http://127.0.0.1:9988",
        )
    finally:
        service.stop()

    profile = load_profile("local", include_token=True)
    assert result["session"] == {"status": "direct"}
    assert profile["connection"] == {
        "api_url": "http://127.0.0.1:9988",
        "mode": "direct",
    }
    assert checked["api_url"] == "http://127.0.0.1:9988"
    assert checked["token"] == profile["credential"]["token"]
    assert profile["credential"]["token"].startswith("mac_client_")
    assert profile["credential"]["token"] not in registry.read_text(encoding="utf-8")
    assert len(ClientPrincipalProvider(registry).tokens()) == 1
    audit = json.loads(
        ClientPrincipalStore(registry).audit_path.read_text(encoding="utf-8").splitlines()[0]
    )
    assert audit["actor"].startswith("local-console:uid=")
    assert ":user=" in audit["actor"]


def test_failed_http_validation_revokes_and_does_not_install(
    tmp_path, short_socket_dir, monkeypatch
):
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    registry = tmp_path / "principals.json"
    service = LocalConsoleService(
        short_socket_dir / "console.sock",
        ClientPrincipalStore(registry),
    )
    monkeypatch.setattr(
        client_login,
        "_validate_token",
        lambda *_args, **_kwargs: (False, "credential_rejected"),
    )
    service.start()
    try:
        with pytest.raises(client_login.ClientLoginError, match="rejected"):
            client_login.local_console_login(
                profile="local",
                client_id="console-client",
                socket_path=str(service.path),
                api_url="http://127.0.0.1:9988",
            )
    finally:
        service.stop()
    assert ClientPrincipalProvider(registry).tokens() == {}
    assert not (tmp_path / "home" / "clients" / "local.yaml").exists()


def test_local_console_renew_validates_then_atomically_replaces_profile(
    tmp_path, short_socket_dir, monkeypatch
):
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    registry = tmp_path / "principals.json"
    service = LocalConsoleService(
        short_socket_dir / "console.sock",
        ClientPrincipalStore(registry),
    )
    checked_tokens = []

    def validate(_api_url, token, *, timeout):
        checked_tokens.append((token, timeout))
        return True, "authenticated"

    monkeypatch.setattr(client_login, "_validate_token", validate)
    service.start()
    try:
        client_login.local_console_login(
            profile="local",
            client_id="console-client",
            socket_path=str(service.path),
            api_url="http://127.0.0.1:9988",
        )
        old_token = load_profile("local", include_token=True)["credential"]["token"]
        result = client_login.renew_local_console_login(
            "local", socket_path=str(service.path), expires_in=7200
        )
    finally:
        service.stop()

    renewed = load_profile("local", include_token=True)
    new_token = renewed["credential"]["token"]
    assert result["status"] == "renewed"
    assert result["changed"] is True
    assert new_token != old_token
    assert checked_tokens[-1][0] == new_token
    assert renewed["credential"]["id"] == "console-client.v2"
    events = [
        json.loads(line)
        for line in ClientPrincipalStore(registry)
        .audit_path.read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["event"] == "client.renewed"
    assert events[-1]["actor"].startswith("local-console:uid=")


def test_local_console_renew_validation_failure_revokes_new_and_keeps_profile(
    tmp_path, short_socket_dir, monkeypatch
):
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "home"))
    registry = tmp_path / "principals.json"
    service = LocalConsoleService(
        short_socket_dir / "console.sock",
        ClientPrincipalStore(registry),
    )
    validation = {"ok": True}
    monkeypatch.setattr(
        client_login,
        "_validate_token",
        lambda *_args, **_kwargs: (
            validation["ok"],
            "authenticated" if validation["ok"] else "credential_rejected",
        ),
    )
    service.start()
    try:
        client_login.local_console_login(
            profile="local",
            client_id="console-client",
            socket_path=str(service.path),
            api_url="http://127.0.0.1:9988",
        )
        old_token = load_profile("local", include_token=True)["credential"]["token"]
        validation["ok"] = False
        with pytest.raises(
            client_login.ClientLoginError,
            match="new credential was revoked.*profile was not replaced",
        ):
            client_login.renew_local_console_login("local", socket_path=str(service.path))
    finally:
        service.stop()

    assert load_profile("local", include_token=True)["credential"]["token"] == old_token
    assert ClientPrincipalProvider(registry).tokens() == {}


def test_missing_socket_error_is_safe_and_useful(tmp_path):
    with pytest.raises(LocalConsoleError, match="socket is missing"):
        request_local_console(_request(), socket_path=str(tmp_path / "missing.sock"))


def test_injected_apps_default_off_and_explicit_socket_tracks_lifespan(tmp_path, short_socket_dir):
    plain = create_app(control_plane=ControlPlane.in_memory(), auth_tokens={"recovery": ["admin"]})
    assert plain.state.local_console_service is None

    socket_path = short_socket_dir / "console.sock"
    app = create_app(
        control_plane=ControlPlane.in_memory(),
        auth_tokens={"recovery": ["admin"]},
        client_principals_path=str(tmp_path / "principals.json"),
        local_console_socket_path=str(socket_path),
    )
    assert not socket_path.exists()
    with TestClient(app):
        assert socket_path.is_socket()
    assert not socket_path.exists()


def test_local_console_credential_hot_reloads_into_http_bearer_auth(tmp_path, short_socket_dir):
    socket_path = short_socket_dir / "console.sock"
    registry = tmp_path / "principals.json"
    app = create_app(
        control_plane=ControlPlane.in_memory(),
        auth_tokens={"recovery": ["admin"]},
        client_principals_path=str(registry),
        local_console_socket_path=str(socket_path),
    )
    with TestClient(app) as client:
        enrolled = request_local_console(_request(), socket_path=str(socket_path))
        old_token = enrolled["credential"]["token"]
        assert (
            client.get("/agents", headers={"Authorization": "Bearer %s" % old_token}).status_code
            == 200
        )

        renewed = request_local_console(
            {"action": "renew", "client_id": "console-client", "expires_in": 7200},
            socket_path=str(socket_path),
        )
        new_token = renewed["credential"]["token"]
        assert new_token != old_token
        assert (
            client.get("/agents", headers={"Authorization": "Bearer %s" % old_token}).status_code
            == 403
        )
        assert (
            client.get("/agents", headers={"Authorization": "Bearer %s" % new_token}).status_code
            == 200
        )


def test_systemd_unit_sets_safe_socket_default_before_operator_environment():
    unit = (Path(__file__).resolve().parents[1] / "deploy" / "systemd" / "mac.service").read_text(
        encoding="utf-8"
    )
    default = "Environment=MAC_LOCAL_CONSOLE_SOCKET=/run/mac/local-console.sock"
    env_file = "EnvironmentFile=/etc/mac/mac.env"
    assert default in unit
    assert unit.index(default) < unit.index(env_file)
    assert "RuntimeDirectory=mac" in unit
    assert "ReadWritePaths=/var/lib/mac /run/mac" in unit
    assert "ProtectHome=true" in unit
