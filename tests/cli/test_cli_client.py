from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac import cli


def _run(tmp_path, *args):
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--json", *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None), err.getvalue()


def test_client_enroll_renew_list_and_revoke_cli(tmp_path):
    registry = tmp_path / "principals.json"
    common = ("--registry", str(registry))

    rc, manifest, _ = _run(
        tmp_path,
        "admin",
        "client",
        "enroll",
        "laptop",
        "--fleet",
        "rocky",
        *common,
    )
    assert rc == 0
    assert manifest["schema"] == "mac.client_enrollment.v1"
    assert manifest["credential"]["token"].startswith("mac_client_")

    rc, renewed, _ = _run(tmp_path, "admin", "client", "renew", "laptop", *common)
    assert rc == 0
    assert renewed["credential"]["token"] != manifest["credential"]["token"]

    rc, clients, _ = _run(tmp_path, "admin", "client", "list", *common)
    assert rc == 0
    assert clients[0]["id"] == "laptop"
    assert "token_hash" not in clients[0]

    rc, revoked, _ = _run(tmp_path, "admin", "client", "revoke", "laptop", *common)
    assert rc == 0
    assert revoked["revoked_at"]


def test_client_enroll_uses_safe_json_when_stdout_is_redirected(tmp_path, capsys):
    registry = tmp_path / "principals.json"

    rc = main(["admin", "client", "enroll", "lost-token", "--registry", str(registry)])

    assert rc == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["credential"]["token"].startswith("mac_client_")
    assert registry.exists()


def test_client_profile_cli_and_fleet_ssh_spec(tmp_path, monkeypatch):
    mac_home = tmp_path / ".mac"
    monkeypatch.setenv("MAC_HOME", str(mac_home))
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema": "mac.client_enrollment.v1",
                "client_id": "laptop",
                "display_name": "Laptop",
                "profile": "rocky",
                "fleet": "rocky",
                "connection": {"api_url": "https://mac.example.test", "mode": "direct"},
                "ssh": {},
                "credential": {
                    "id": "laptop.v1",
                    "token": "mac_client_cli_secure_token_value_123456",
                    "scopes": ["read"],
                    "issued_at": "",
                    "expires_at": "",
                },
                "capabilities": [],
            }
        ),
        encoding="utf-8",
    )
    rc, installed, _ = _run(tmp_path, "admin", "client", "profile", "install", str(manifest_path))
    assert rc == 0 and installed["profile"] == "rocky"
    rc, shown, _ = _run(tmp_path, "admin", "client", "profile", "show", "rocky")
    assert rc == 0 and shown["credential"]["stored"] is True

    fleets = tmp_path / "fleets.yaml"
    fleets.write_text(
        "fleets:\n  rocky:\n    hub_agent: hub\n    agents:\n      - name: hub\n        target: ops@hub.example\n",
        encoding="utf-8",
    )
    rc, spec, _ = _run(
        tmp_path,
        "admin",
        "fleet",
        "ssh-spec",
        "--fleet",
        "rocky",
        "--fleets-config",
        str(fleets),
    )
    assert rc == 0 and spec["target"] == "ops@hub.example"


def test_client_renew_if_due_cli_reports_profiles(tmp_path, monkeypatch):
    """`client renew-if-due` is the timer-safe entry point for C4 renewal.

    A host with no installed profiles must exit cleanly with an empty report
    rather than erroring: the command runs unattended on a schedule, so "there
    is nothing to renew" is a normal outcome, not a failure.
    """
    monkeypatch.setenv("MAC_HOME", str(tmp_path / ".mac"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAC_CLIENT_PROFILES_DIR", raising=False)
    monkeypatch.delenv("MAC_CLIENT_CREDENTIALS_DIR", raising=False)

    rc, report, _ = _run(tmp_path, "admin", "client", "renew-if-due", "--dry-run")

    assert rc == 0
    assert report["schema"] == "mac.credential_renewal.report.v1"
    assert report["profiles"] == []
    # The renewal point is a configured fraction of the credential lifetime,
    # not a fixed lead time.
    assert 0 < report["renew_at_fraction"] < 1
