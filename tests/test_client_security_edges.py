"""Security and malformed-input coverage for client bootstrap contracts."""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

from mac import client_principals as principals
from mac import client_profiles as profiles
from mac import fleet_ssh


def _manifest(**overrides):
    value = {
        "schema": principals.MANIFEST_SCHEMA,
        "client_id": "laptop",
        "profile": "rocky",
        "fleet": "rocky",
        "connection": {"api_url": "https://hub.example", "mode": "direct"},
        "ssh": {},
        "credential": {
            "id": "laptop.v1",
            "token": "mac_client_secure_token_value_1234567890",
            "scopes": ["read"],
        },
    }
    value.update(overrides)
    return value


@pytest.fixture
def client_home(tmp_path, monkeypatch):
    home = tmp_path / ".mac"
    monkeypatch.setenv("MAC_HOME", str(home))
    monkeypatch.delenv("MAC_CLIENT_PROFILES_DIR", raising=False)
    monkeypatch.delenv("MAC_CLIENT_CREDENTIALS_DIR", raising=False)
    return home


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda m: m.update(schema="wrong"), "manifest schema"),
        (lambda m: m.update(profile="bad/name"), "must match"),
        (lambda m: m.update(connection=[]), "must be an object"),
        (lambda m: m.update(connection={"api_url": "ftp://host"}), r"http\(s\) URL"),
        (lambda m: m.update(connection={"api_url": "https://u:p@host"}), "must not contain"),
        (lambda m: m.update(connection={"api_url": "https://host", "mode": "bad"}), "direct or ssh"),
        (lambda m: m.update(connection={"api_url": "https://host", "mode": "ssh-tunnel"}), "require ssh.target"),
        (lambda m: m.update(ssh={"host_key_policy": "bad"}), "host_key_policy"),
        (lambda m: m["credential"].update(token="short"), "token is missing"),
        (lambda m: m["credential"].update(id=""), "credential.id"),
        (lambda m: m["credential"].update(scopes=[]), "scopes must be non-empty"),
        (lambda m: m["connection"].update(local_port="bad"), "must be an integer"),
        (lambda m: m["connection"].update(remote_port=70000), "between 1 and 65535"),
    ],
)
def test_manifest_validation_rejects_malformed_fields(mutation, message: str) -> None:
    manifest = _manifest()
    mutation(manifest)
    with pytest.raises(profiles.ClientProfileError, match=message):
        profiles.validate_enrollment_manifest(manifest)


def test_manifest_validation_normalizes_tunnel_ports_and_fields() -> None:
    manifest = _manifest(
        connection={
            "api_url": "http://127.0.0.1:8000/",
            "mode": "ssh-tunnel",
            "local_port": "9000",
            "remote_port": 8789,
            "remote_host": " localhost ",
        },
        ssh={
            "target": "ops@hub",
            "port": "2222",
            "host_key_policy": "accept-new",
        },
        capabilities=[" tasks ", "tasks"],
    )
    normalized = profiles.validate_enrollment_manifest(manifest)["profile"]
    assert normalized["connection"] == {
        "api_url": "http://127.0.0.1:8000",
        "mode": "ssh-tunnel",
        "local_port": 9000,
        "remote_port": 8789,
        "remote_host": "localhost",
    }
    assert normalized["ssh"]["port"] == 2222
    assert normalized["capabilities"] == ["tasks"]


def test_stored_profile_rejects_secrets_permissions_and_escape(client_home) -> None:
    root = profiles.clients_root()
    root.mkdir(parents=True)
    path = root / "rocky.yaml"
    path.write_text("not: [yaml")
    path.chmod(0o600)
    with pytest.raises(profiles.ClientProfileError, match="could not read"):
        profiles._read_yaml(path)
    path.write_text(yaml.safe_dump({"schema": "wrong"}))
    with pytest.raises(profiles.ClientProfileError, match="is not"):
        profiles._read_yaml(path)
    stored = profiles.validate_enrollment_manifest(_manifest())["profile"]
    stored["credential"]["path"] = "../credentials/clients/x.token"
    stored["credential"]["token"] = "must-not-be-stored"
    path.write_text(yaml.safe_dump(stored))
    with pytest.raises(profiles.ClientProfileError, match="unsupported field"):
        profiles._read_yaml(path)
    path.chmod(0o644)
    with pytest.raises(profiles.ClientProfileError, match="permissions"):
        profiles._read_yaml(path)

    with pytest.raises(profiles.ClientProfileError, match="no credential reference"):
        profiles._credential_path_from_profile({"credential": {}})
    with pytest.raises(profiles.ClientProfileError, match="escapes"):
        profiles._credential_path_from_profile({"credential": {"path": "../../outside"}})


def test_profile_load_activation_removal_and_manifest_reader(client_home, monkeypatch, tmp_path) -> None:
    with pytest.raises(profiles.ClientProfileError, match="does not exist"):
        profiles.activate_profile("missing")
    with pytest.raises(profiles.ClientProfileError, match="no active"):
        profiles.load_profile()

    profiles.install_enrollment_manifest(_manifest(), activate=False)
    assert profiles.load_profile()["profile"] == "rocky"
    profiles.activate_profile("rocky")
    result = profiles.remove_profile("rocky")
    assert result == {"profile": "rocky", "removed": True}
    assert not list(profiles.credentials_root().glob("*.token"))
    assert profiles.active_profile_name() is None

    bad = tmp_path / "bad.json"
    bad.write_text("not-json")
    with pytest.raises(profiles.ClientProfileError, match="must be JSON"):
        profiles.read_manifest(str(bad))
    bad.write_text("[]")
    with pytest.raises(profiles.ClientProfileError, match="must be an object"):
        profiles.read_manifest(str(bad))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"ok":true}'))
    assert profiles.read_manifest("-") == {"ok": True}


def test_missing_profile_credential_is_reported(client_home) -> None:
    profiles.install_enrollment_manifest(_manifest())
    next(profiles.credentials_root().glob("*.token")).unlink()
    with pytest.raises(profiles.ClientProfileError, match="credential record is missing"):
        profiles.load_profile("rocky", include_token=True)


def test_principal_timestamp_scope_and_id_validation() -> None:
    assert principals._parse_timestamp("") is None
    naive = principals._parse_timestamp("2026-01-01T00:00:00")
    assert naive.tzinfo is timezone.utc
    with pytest.raises(principals.ClientPrincipalError, match="invalid timestamp"):
        principals._parse_timestamp("bad")
    with pytest.raises(principals.ClientPrincipalError, match="client id"):
        principals._validate_id("bad/id")
    with pytest.raises(principals.ClientPrincipalError, match="at least one"):
        principals.normalize_scopes(iter(()))
    with pytest.raises(principals.ClientPrincipalError, match="unknown"):
        principals.normalize_scopes(["unknown"])


def test_principal_store_rejects_invalid_registry_and_lifecycle_edges(tmp_path) -> None:
    path = tmp_path / "principals.json"
    store = principals.ClientPrincipalStore(path)
    path.write_text("not-json")
    path.chmod(0o600)
    with pytest.raises(principals.ClientPrincipalError, match="could not read"):
        store.read()
    path.write_text(json.dumps({"schema": "wrong", "clients": {}}))
    with pytest.raises(principals.ClientPrincipalError, match="is not"):
        store.read()
    path.write_text(json.dumps({"schema": principals.REGISTRY_SCHEMA, "clients": []}))
    with pytest.raises(principals.ClientPrincipalError, match="clients must be"):
        store.read()
    path.unlink()

    with pytest.raises(principals.ClientPrincipalError, match="at least 60"):
        store.enroll("client", expires_in=1)
    with pytest.raises(principals.ClientPrincipalError, match="does not exist"):
        store.renew("client")
    with pytest.raises(principals.ClientPrincipalError, match="does not exist"):
        store.revoke("client")
    issued = store.enroll("client")
    rotated = store.enroll("client", rotate=True)
    assert rotated.record["credential_version"] == 2
    store.revoke("client")
    assert store.revoke("client")["id"] == "client"
    with pytest.raises(principals.ClientPrincipalError, match="is revoked"):
        store.renew("client")
    assert issued.token != rotated.token


def test_active_principal_mapping_filters_invalid_expired_and_revoked() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = (now + timedelta(days=1)).isoformat()
    registry = {
        "clients": {
            "not-map": "bad",
            "revoked": {"revoked_at": future},
            "bad-time": {"expires_at": "bad"},
            "expired": {"expires_at": now.isoformat()},
            "bad-hash": {"expires_at": future, "token_hash": "bad", "scopes": ["read"]},
            "no-scopes": {"expires_at": future, "token_hash": "sha256:x", "scopes": []},
            "active": {
                "id": "active",
                "expires_at": future,
                "token_hash": "sha256:good",
                "scopes": ["read"],
            },
        }
    }
    assert principals._active_mapping_from_registry(registry, now=now) == {
        "sha256:good": {"scopes": ["read"], "client_id": "active"}
    }
    assert principals._active_mapping_from_registry([], now=now) == {}


def test_fleet_registry_and_route_error_matrix(tmp_path) -> None:
    with pytest.raises(fleet_ssh.FleetSshError, match="integer"):
        fleet_ssh._optional_int("bad", field="port")
    with pytest.raises(fleet_ssh.FleetSshError, match="between"):
        fleet_ssh._optional_int(0, field="port")
    assert fleet_ssh._normalize_agents({"a": {"target": "h"}, "bad": []}) == {
        "a": {"target": "h"}
    }
    assert fleet_ssh._normalize_agents(["bad", {"name": "", "target": "h"}]) == {}
    with pytest.raises(fleet_ssh.FleetSshError, match="require hub_agent"):
        fleet_ssh.fleet_entries({"fleets": [{}]})
    with pytest.raises(fleet_ssh.FleetSshError, match="duplicate"):
        fleet_ssh.fleet_entries({"fleets": [{"hub_agent": "a"}, {"hub_agent": "a"}]})
    with pytest.raises(fleet_ssh.FleetSshError, match="no fleets"):
        fleet_ssh.resolve_fleet_key({}, None)

    multi = {
        "fleets": {
            "one": {"fleet_name": "shared", "hub_agent": "hub", "agents": []},
            "two": {"fleet_name": "shared", "hub_agent": "hub2", "agents": []},
        }
    }
    with pytest.raises(fleet_ssh.FleetSshError, match="multiple fleets"):
        fleet_ssh.resolve_fleet_key(multi, None)
    with pytest.raises(fleet_ssh.FleetSshError, match="not found"):
        fleet_ssh.resolve_fleet_key(multi, "shared")
    multi["fleets"]["two"]["default"] = True
    assert fleet_ssh.resolve_fleet_key(multi, None) == "two"

    base = {"fleets": {"one": {"agents": []}}}
    with pytest.raises(fleet_ssh.FleetSshError, match="no hub_agent"):
        fleet_ssh.resolve_fleet_ssh(base, "one")
    base["fleets"]["one"]["hub_agent"] = "hub"
    with pytest.raises(fleet_ssh.FleetSshError, match="not in fleet"):
        fleet_ssh.resolve_fleet_ssh(base, "one")
    base["fleets"]["one"]["agents"] = [{"name": "hub"}]
    with pytest.raises(fleet_ssh.FleetSshError, match="has no target"):
        fleet_ssh.resolve_fleet_ssh(base, "one")
    base["fleets"]["one"]["agents"][0]["target"] = "host"
    base["fleets"]["one"]["defaults"] = {"ssh_host_key_policy": "bad"}
    with pytest.raises(fleet_ssh.FleetSshError, match="must be one of"):
        fleet_ssh.resolve_fleet_ssh(base, "one")

    missing = tmp_path / "missing.yaml"
    with pytest.raises(fleet_ssh.FleetSshError, match="not found"):
        fleet_ssh.load_fleet_config(str(missing))
    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("[")
    with pytest.raises(fleet_ssh.FleetSshError, match="could not parse"):
        fleet_ssh.load_fleet_config(str(invalid))
    invalid.write_text("value")
    with pytest.raises(fleet_ssh.FleetSshError, match="unexpected"):
        fleet_ssh.load_fleet_config(str(invalid))


def test_fleet_route_options_cover_insecure_host_ca_and_identity_refs(tmp_path) -> None:
    base = dict(
        fleet="f",
        fleet_name="f",
        agent="a",
        target="host",
        port=None,
        proxy_jump=None,
        identity_file=None,
        identity_ref=None,
        known_hosts_file=None,
        host_key_policy="insecure",
        host_key_fingerprint=None,
        host_ca=None,
        supervisor="auto",
        os_kind="linux",
        control_port=8789,
    )
    insecure = fleet_ssh.FleetSshSpec(**base)
    values = fleet_ssh.route_argv(insecure, batch_mode=False, connect_timeout=0)
    assert "StrictHostKeyChecking=no" in values
    assert "UserKnownHostsFile=/dev/null" in values
    with pytest.raises(fleet_ssh.FleetSshError, match="kind"):
        fleet_ssh.route_argv(insecure, kind="bad")

    ref = fleet_ssh.FleetSshSpec(**{**base, "identity_ref": "vault:key"})
    with pytest.raises(fleet_ssh.FleetSshError, match="cannot be converted"):
        fleet_ssh.ssh_argv(ref)
    ca = fleet_ssh.FleetSshSpec(
        **{**base, "host_key_policy": "strict", "host_ca": str(tmp_path / "ca")}
    )
    assert "UserKnownHostsFile=%s" % (tmp_path / "ca") in fleet_ssh.ssh_argv(ca)


def test_fleet_cli_reports_resolution_errors(capsys) -> None:
    assert fleet_ssh.main(["--config", "/definitely/missing"]) == 2
    assert "fleets config not found" in capsys.readouterr().err
