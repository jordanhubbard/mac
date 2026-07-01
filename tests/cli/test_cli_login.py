from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

from mac import client_login
from mac.cli import main


def _run(_tmp_path, *args):
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--json", *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None, err.getvalue()


def test_login_cli_enroll_status_and_renew(tmp_path, monkeypatch):
    spec = SimpleNamespace(control_port=8789)
    captured = {}
    monkeypatch.setattr(
        client_login,
        "resolve_login_spec",
        lambda **kwargs: captured.update(resolve=kwargs) or spec,
    )
    monkeypatch.setattr(
        client_login,
        "login",
        lambda **kwargs: captured.update(login=kwargs)
        or {"status": "logged_in", "profile": kwargs["profile"]},
    )
    rc, result, _ = _run(tmp_path, "login")
    assert rc == 0 and result["status"] == "logged_in"
    rc, result, _ = _run(
        tmp_path,
        "login",
        "--ssh",
        "mac@hub",
        "--identity-file",
        "/key",
        "--known-hosts-file",
        "/known",
        "--profile",
        "prod",
        "--client-id",
        "laptop",
    )
    assert rc == 0 and result == {"profile": "prod", "status": "logged_in"}
    assert captured["resolve"]["ssh_target"] == "mac@hub"
    assert captured["login"]["client_id"] == "laptop"

    monkeypatch.setattr(
        client_login,
        "login_status",
        lambda profile: {"status": "connected", "profile": profile},
    )
    rc, result, _ = _run(tmp_path, "login", "status", "--profile", "prod")
    assert rc == 0 and result["status"] == "connected"

    monkeypatch.setattr(
        client_login,
        "renew_login",
        lambda profile, **_kwargs: {"status": "renewed", "profile": profile},
    )
    rc, result, _ = _run(tmp_path, "login", "renew", "--profile", "prod")
    assert rc == 0 and result["status"] == "renewed"


def test_logout_cli_and_secret_safe_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        client_login,
        "logout",
        lambda profile, **kwargs: {
            "status": "logged_out",
            "profile": profile,
            "revoked": kwargs["revoke"],
        },
    )
    rc, result, _ = _run(tmp_path, "logout")
    assert rc == 0 and result["status"] == "logged_out"
    rc, result, _ = _run(tmp_path, "logout", "--profile", "prod", "--revoke")
    assert rc == 0 and result["revoked"] is True

    monkeypatch.setattr(
        client_login,
        "login_status",
        lambda _profile: (_ for _ in ()).throw(
            client_login.ClientLoginError("credential rejected")
        ),
    )
    rc, result, error = _run(
        tmp_path, "login", "status", "--profile", "prod"
    )
    assert rc == 1 and result is None
    assert error.strip() == "credential rejected"
