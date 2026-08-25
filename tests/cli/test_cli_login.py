from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

from mac import cli as mac_cli
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
        lambda **kwargs: (
            captured.update(login=kwargs) or {"status": "logged_in", "profile": kwargs["profile"]}
        ),
    )
    rc, result, _ = _run(tmp_path, "admin", "login")
    assert rc == 0 and result["status"] == "logged_in"
    rc, result, _ = _run(
        tmp_path,
        "admin",
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
    rc, result, _ = _run(tmp_path, "admin", "login", "status", "--profile", "prod")
    assert rc == 0 and result["status"] == "connected"

    monkeypatch.setattr(
        client_login,
        "renew_login",
        lambda profile, **_kwargs: {"status": "renewed", "profile": profile},
    )
    rc, result, _ = _run(tmp_path, "admin", "login", "renew", "--profile", "prod")
    assert rc == 0 and result["status"] == "renewed"


def test_login_cli_local_console_uses_no_ssh_resolution(tmp_path, monkeypatch):
    captured = {}
    monkeypatch.setattr(
        client_login,
        "resolve_login_spec",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("SSH was consulted")),
    )
    monkeypatch.setattr(
        client_login,
        "local_console_login",
        lambda **kwargs: (
            captured.update(kwargs) or {"status": "logged_in", "profile": kwargs["profile"]}
        ),
    )
    rc, result, error = _run(
        tmp_path,
        "admin",
        "login",
        "--local-console",
        "--local-console-socket",
        "/run/mac/custom.sock",
        "--api-url",
        "http://127.0.0.1:9999",
        "--profile",
        "console",
    )
    assert rc == 0 and not error
    assert result == {"profile": "console", "status": "logged_in"}
    assert captured["socket_path"] == "/run/mac/custom.sock"
    assert captured["api_url"] == "http://127.0.0.1:9999"

    monkeypatch.setattr(
        client_login,
        "renew_local_console_login",
        lambda profile, **kwargs: (
            captured.update(renew=(profile, kwargs)) or {"status": "renewed", "profile": profile}
        ),
    )
    rc, result, error = _run(
        tmp_path,
        "admin",
        "login",
        "renew",
        "--local-console",
        "--local-console-socket",
        "/run/mac/custom.sock",
        "--profile",
        "console",
    )
    assert rc == 0 and not error
    assert result == {"profile": "console", "status": "renewed"}
    assert captured["renew"][1]["socket_path"] == "/run/mac/custom.sock"

    monkeypatch.setattr(
        client_login,
        "login_status",
        lambda profile: {"status": "connected", "profile": profile},
    )
    rc, result, error = _run(
        tmp_path,
        "admin",
        "login",
        "status",
        "--local-console",
        "--profile",
        "console",
    )
    assert rc == 0 and not error
    assert result == {"profile": "console", "status": "connected"}


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
    rc, result, _ = _run(tmp_path, "admin", "logout")
    assert rc == 0 and result["status"] == "logged_out"
    rc, result, _ = _run(tmp_path, "admin", "logout", "--profile", "prod", "--revoke")
    assert rc == 0 and result["revoked"] is True

    monkeypatch.setattr(
        client_login,
        "login_status",
        lambda _profile: (_ for _ in ()).throw(
            client_login.ClientLoginError("credential rejected")
        ),
    )
    rc, result, error = _run(tmp_path, "admin", "login", "status", "--profile", "prod")
    assert rc == 1 and result is None
    assert error.strip() == "credential rejected"
