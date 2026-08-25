from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from mac.client_principals import MANIFEST_SCHEMA
from mac.client_profiles import (
    ClientProfileError,
    active_profile_name,
    install_enrollment_manifest,
    list_profiles,
    load_profile,
    migrate_legacy_profile,
    show_profile,
)
from mac.dispatch import RemoteDispatch, resolve_dispatch


def _manifest(**overrides):
    value = {
        "schema": MANIFEST_SCHEMA,
        "client_id": "laptop",
        "display_name": "Laptop",
        "profile": "rocky",
        "fleet": "rocky",
        "connection": {"api_url": "https://mac.example.test", "mode": "direct"},
        "ssh": {},
        "credential": {
            "id": "laptop.v1",
            "token": "mac_client_a_secure_token_value_1234567890",
            "scopes": ["admin", "dispatch", "read", "write"],
            "issued_at": "2026-06-30T00:00:00+00:00",
            "expires_at": "2026-07-30T00:00:00+00:00",
        },
        "capabilities": ["tasks"],
    }
    value.update(overrides)
    return value


@pytest.fixture
def isolated_mac_home(tmp_path, monkeypatch):
    home = tmp_path / ".mac"
    monkeypatch.setenv("MAC_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAC_CLIENT_PROFILES_DIR", raising=False)
    monkeypatch.delenv("MAC_CLIENT_CREDENTIALS_DIR", raising=False)
    return home


def test_install_separates_secret_and_redacts_show(isolated_mac_home):
    token = _manifest()["credential"]["token"]

    result = install_enrollment_manifest(_manifest())
    profile_path = Path(result["profile_path"])
    stored = load_profile("rocky", include_token=True)
    shown = show_profile("rocky")

    assert result["active"] is True
    assert active_profile_name() == "rocky"
    assert token not in profile_path.read_text(encoding="utf-8")
    assert stored["credential"]["token"] == token
    assert "token" not in shown["credential"]
    assert "path" not in shown["credential"]
    assert shown["credential"]["stored"] is True
    assert profile_path.stat().st_mode & 0o777 == 0o600
    credential = next((isolated_mac_home / "credentials" / "clients").glob("*.token"))
    assert credential.stat().st_mode & 0o777 == 0o600
    token_hits = [
        path
        for path in isolated_mac_home.rglob("*")
        if path.is_file() and token in path.read_text(encoding="utf-8")
    ]
    assert token_hits == [credential]


def test_install_is_idempotent_and_update_has_secure_backup(isolated_mac_home):
    first = install_enrollment_manifest(_manifest())
    again = install_enrollment_manifest(_manifest())
    changed_manifest = _manifest()
    changed_manifest["credential"]["id"] = "laptop.v2"
    changed_manifest["credential"]["token"] = "mac_client_rotated_secure_token_1234567890"
    updated = install_enrollment_manifest(changed_manifest)

    assert first["changed"] is True
    assert again["changed"] is False
    assert updated["changed"] is True
    assert Path(updated["backup"]).is_dir()
    assert all(path.stat().st_mode & 0o077 == 0 for path in Path(updated["backup"]).iterdir())
    assert len(list_profiles()) == 1


def test_manifest_rejects_embedded_secret_fields(isolated_mac_home):
    manifest = _manifest()
    manifest["MAC_SECRET_KEY"] = "never"

    with pytest.raises(ClientProfileError, match="unsupported field"):
        install_enrollment_manifest(manifest)


def test_strict_ssh_profile_requires_pinned_identity(isolated_mac_home):
    manifest = _manifest(
        connection={
            "api_url": "http://127.0.0.1:49123",
            "mode": "ssh-tunnel",
        },
        ssh={"target": "ops@hub.example", "host_key_policy": "strict"},
    )

    with pytest.raises(ClientProfileError, match="known_hosts"):
        install_enrollment_manifest(manifest)


def test_dispatch_uses_active_profile_without_legacy_files(isolated_mac_home, monkeypatch):
    install_enrollment_manifest(_manifest())
    for name in (
        "MAC_API_URL",
        "MAC_URL",
        "MAC_HUB_URL",
        "HGMAC_URL",
        "MAC_API_TOKEN",
        "MAC_DB",
        "MAC_FLEET",
        "MAC_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(isolated_mac_home / "absent.yaml"))
    args = argparse.Namespace(db=None, hub_url=None, token=None, fleet=None, profile=None)

    dispatch = resolve_dispatch(args)

    assert isinstance(dispatch, RemoteDispatch)
    assert dispatch._client.base_url == "https://mac.example.test"
    assert dispatch._client.token == _manifest()["credential"]["token"]


def test_dispatch_reconnects_ssh_tunnel_profile(isolated_mac_home, monkeypatch):
    manifest = _manifest(
        connection={
            "api_url": "http://127.0.0.1:48789",
            "mode": "ssh-tunnel",
            "local_port": 48789,
            "remote_host": "127.0.0.1",
            "remote_port": 8789,
        },
        ssh={
            "target": "mac@hub.example",
            "identity_file": "/private/key",
            "known_hosts_file": "/private/known_hosts",
            "host_key_policy": "strict",
        },
    )
    install_enrollment_manifest(manifest)
    called = []
    monkeypatch.setattr("mac.client_login.ensure_session", lambda profile: called.append(profile))
    for name in ("MAC_API_URL", "MAC_URL", "MAC_HUB_URL", "MAC_API_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MAC_DEPLOY_ENV_FILE", "/dev/null")
    args = argparse.Namespace(db=None, hub_url=None, token=None, fleet=None, profile=None)

    dispatch = resolve_dispatch(args)

    assert isinstance(dispatch, RemoteDispatch)
    assert called == ["rocky"]


def test_legacy_migration_requires_explicit_admin_acknowledgement(isolated_mac_home, monkeypatch):
    fleets = isolated_mac_home / "fleets.yaml"
    env = isolated_mac_home / ".env"
    isolated_mac_home.mkdir(parents=True)
    fleets.write_text(
        """fleets:
  rocky:
    hub_agent: rocky
    hub_url: https://mac.example.test
    defaults:
      identity_file: ~/.ssh/rocky
      ssh_known_hosts_file: ~/.ssh/known_hosts
    agents:
      - name: rocky
        target: ops@rocky.example
""",
        encoding="utf-8",
    )
    env.write_text("MAC_API_TOKEN__ROCKY=legacy-admin-token-value-123456\n", encoding="utf-8")
    env.chmod(0o600)

    with pytest.raises(ClientProfileError, match="administrator authority"):
        migrate_legacy_profile(fleet="rocky", fleets_config=str(fleets), env_file=str(env))

    result = migrate_legacy_profile(
        fleet="rocky",
        fleets_config=str(fleets),
        env_file=str(env),
        allow_legacy_admin_token=True,
    )

    assert result["legacy_admin"] is True
    assert Path(result["legacy_backup"]).is_dir()
    assert show_profile("rocky")["credential"]["scopes"] == ["admin"]

    again = migrate_legacy_profile(
        fleet="rocky",
        fleets_config=str(fleets),
        env_file=str(env),
        allow_legacy_admin_token=True,
    )
    assert again["changed"] is False
    assert "legacy_backup" not in again
