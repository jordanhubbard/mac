"""Credentials must not sever the fleet when they lapse (C1-C4).

Covers the four defects recorded in docs/peer-repair-design.md §7.2/§7.3:
naming expiry instead of reporting it as an unknown token, reporting expiry
before it bites, making the lifetime a fleet setting, and renewing before the
cliff over the SSH trust root.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac.client_principals import (
    DEFAULT_CREDENTIAL_TTL_SECONDS,
    MANIFEST_SCHEMA,
    REGISTRY_SCHEMA,
    ClientPrincipalProvider,
    _expiry_reason_from_registry,
    _token_hash,
    configured_credential_ttl_seconds,
)
from mac.credential_renewal import (
    DEFAULT_RENEW_AT_FRACTION,
    configured_renew_fraction,
    renew_due_profiles,
    renew_profile,
    renewal_status,
)

NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)
TOKEN = "mac_client_a_secure_token_value_1234567890"


def _registry(expires_at: str, *, revoked_at: str | None = None, token: str = TOKEN) -> dict:
    record = {
        "id": "jkh-hub-admin",
        "token_hash": _token_hash(token),
        "expires_at": expires_at,
        "scopes": ["read"],
    }
    if revoked_at:
        record["revoked_at"] = revoked_at
    return {"clients": {"jkh-hub-admin": record}}


# --- C1: an expired credential says so, instead of "unknown bearer token" ---


def test_expired_credential_is_named_as_expired():
    detail = _expiry_reason_from_registry(
        _registry("2026-09-22T12:00:00+00:00"), _token_hash(TOKEN), now=NOW
    )
    assert detail is not None
    assert detail["reason"] == "expired"
    assert detail["client_id"] == "jkh-hub-admin"
    assert detail["expires_at"].startswith("2026-09-22")


@pytest.mark.parametrize(
    "registry, token, why",
    [
        (_registry("2026-09-24T12:00:00+00:00"), TOKEN, "still valid"),
        (_registry("2026-09-22T12:00:00+00:00", revoked_at="2026-09-01T00:00:00+00:00"), TOKEN,
         "revoked stays indistinguishable from unknown"),
        (_registry("2026-09-22T12:00:00+00:00"), "some-other-token", "unknown token"),
    ],
)
def test_only_expiry_is_disclosed(registry, token, why):
    assert _expiry_reason_from_registry(registry, _token_hash(token), now=NOW) is None, why


def test_authorize_request_names_expiry_and_the_right_remedy():
    from mac.api import _authorize_request
    from mac.models import AuthorizationError

    def explain(token):
        return {"reason": "expired", "client_id": "jkh-hub-admin", "expires_at": "2026-09-23T09:24:24+00:00"}

    with pytest.raises(AuthorizationError) as excinfo:
        _authorize_request(
            "GET", "/tasks", "Bearer " + TOKEN, {"other": object()}, explain_expired=explain
        )
    message = str(excinfo.value)
    assert "expired bearer token" in message
    assert "jkh-hub-admin" in message
    # The operator must not be sent to repair token drift, which is the
    # documented cause of a 403 and the wrong layer entirely here.
    assert "mac admin client renew" in message
    assert "sync-token` will not fix it" in message


def test_unknown_token_is_still_unknown():
    from mac.api import _authorize_request
    from mac.models import AuthorizationError

    with pytest.raises(AuthorizationError) as excinfo:
        _authorize_request(
            "GET", "/tasks", "Bearer " + TOKEN, {"other": object()}, explain_expired=lambda _t: None
        )
    assert str(excinfo.value) == "unknown bearer token"


def test_provider_explain_expired_reads_the_registry(tmp_path):
    path = tmp_path / "client-principals.json"
    registry = {**_registry("2026-09-22T12:00:00+00:00"), "schema": REGISTRY_SCHEMA}
    path.write_text(json.dumps(registry), encoding="utf-8")
    path.chmod(0o600)
    provider = ClientPrincipalProvider(path)
    provider.tokens(now=NOW)  # prime the mtime cache the way the hub does
    assert provider.explain_expired(TOKEN, now=NOW)["reason"] == "expired"
    assert provider.explain_expired("not-a-real-token", now=NOW) is None


# --- C2: expiry is reported before it bites ---


def _diag_registry(tmp_path, monkeypatch, records):
    path = tmp_path / "client-principals.json"
    path.write_text(
        json.dumps({"schema": REGISTRY_SCHEMA, "clients": records}), encoding="utf-8"
    )
    path.chmod(0o600)
    monkeypatch.setenv("MAC_CLIENT_PRINCIPALS_FILE", str(path))
    return path


def _client(name, delta_days):
    when = datetime.now(timezone.utc) + timedelta(days=delta_days)
    return {"id": name, "expires_at": when.isoformat(), "scopes": ["read"], "token_hash": "sha256:x"}


def test_credential_expiry_check_is_quiet_when_healthy(tmp_path, monkeypatch):
    from mac.diagnostics import _credential_expiry

    _diag_registry(tmp_path, monkeypatch, {"a": _client("a", 20)})
    findings = _credential_expiry(None)
    assert [f.severity for f in findings] == ["ok"]


def test_credential_expiry_check_warns_before_expiry(tmp_path, monkeypatch):
    from mac.diagnostics import _credential_expiry

    _diag_registry(tmp_path, monkeypatch, {"a": _client("soon", 2), "b": _client("fine", 20)})
    finding = _credential_expiry(None)[0]
    assert finding.severity == "warn"
    assert [e["client_id"] for e in finding.detail["expiring"]] == ["soon"]
    assert "mac admin client renew" in finding.detail["remediation"]


def test_credential_expiry_check_reports_already_expired(tmp_path, monkeypatch):
    from mac.diagnostics import _credential_expiry

    _diag_registry(tmp_path, monkeypatch, {"a": _client("dead", -5), "b": _client("soon", 2)})
    finding = _credential_expiry(None)[0]
    # `warn`, not `error`: every sibling expiry/staleness check in this
    # framework warns, and `summarize` treats any error as "report not ok".
    assert finding.severity == "warn"
    assert [e["client_id"] for e in finding.detail["expired"]] == ["dead"]
    assert [e["client_id"] for e in finding.detail["expiring"]] == ["soon"]


def test_revoked_credentials_are_not_reported_as_expired(tmp_path, monkeypatch):
    from mac.diagnostics import _credential_expiry

    record = _client("revoked", -5)
    record["revoked_at"] = "2026-01-01T00:00:00+00:00"
    _diag_registry(tmp_path, monkeypatch, {"a": record})
    assert _credential_expiry(None)[0].severity == "ok"


def test_credential_expiry_is_a_registered_diagnostic():
    from mac.diagnostics import CHECKS

    assert any(check.name == "credential-expiry" for check in CHECKS)



# --- C3: the lifetime is a fleet setting, not a hardcoded literal ---


@pytest.mark.parametrize(
    "value, expected, why",
    [
        (None, DEFAULT_CREDENTIAL_TTL_SECONDS, "unset falls back to 30 days"),
        ("604800", 604800, "a valid value is honoured"),
        ("not-a-number", DEFAULT_CREDENTIAL_TTL_SECONDS, "garbage falls back"),
        ("5", DEFAULT_CREDENTIAL_TTL_SECONDS, "below the floor falls back"),
        ("999999999999", DEFAULT_CREDENTIAL_TTL_SECONDS, "above the ceiling falls back"),
    ],
)
def test_configured_credential_ttl(value, expected, why):
    env = {} if value is None else {"MAC_CLIENT_CREDENTIAL_TTL_SECONDS": value}
    assert configured_credential_ttl_seconds(env) == expected, why


def test_cli_expires_in_defaults_come_from_the_setting(monkeypatch):
    from mac.cli import build_parser

    monkeypatch.setenv("MAC_CLIENT_CREDENTIAL_TTL_SECONDS", "604800")
    parser = build_parser()
    for argv in (
        ["admin", "client", "renew", "some-client"],
        ["admin", "client", "enroll", "some-client"],
    ):
        assert parser.parse_args(argv).expires_in == 604800


def test_explicit_expires_in_still_wins(monkeypatch):
    from mac.cli import build_parser

    monkeypatch.setenv("MAC_CLIENT_CREDENTIAL_TTL_SECONDS", "604800")
    args = build_parser().parse_args(
        ["admin", "client", "renew", "some-client", "--expires-in", "3600"]
    )
    assert args.expires_in == 3600


# --- C4: renewal happens before the cliff, over the SSH trust root ---


def test_renewal_becomes_due_at_the_configured_fraction():
    credential = {
        "issued_at": "2026-09-01T00:00:00+00:00",
        "expires_at": "2026-10-01T00:00:00+00:00",
    }
    early = renewal_status(credential, now=datetime(2026, 9, 10, tzinfo=timezone.utc))
    at_point = renewal_status(credential, now=datetime(2026, 9, 16, tzinfo=timezone.utc))

    assert early["due"] is False
    assert at_point["due"] is True
    # Due well before expiry: a failing renewal is reported while the current
    # credential still authenticates.
    assert at_point["expired"] is False
    assert at_point["remaining_seconds"] > 0


def test_expired_credential_is_both_due_and_expired():
    status = renewal_status(
        {"issued_at": "2026-09-01T00:00:00+00:00", "expires_at": "2026-09-10T00:00:00+00:00"},
        now=NOW,
    )
    assert status["due"] is True
    assert status["expired"] is True
    assert status["remaining_seconds"] < 0


def test_unusable_timestamps_are_repaired_not_trusted():
    assert renewal_status({})["due"] is True


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, DEFAULT_RENEW_AT_FRACTION),
        ("0.25", 0.25),
        ("nonsense", DEFAULT_RENEW_AT_FRACTION),
        ("0.0", DEFAULT_RENEW_AT_FRACTION),
        ("1.5", DEFAULT_RENEW_AT_FRACTION),
    ],
)
def test_configured_renew_fraction(value, expected):
    env = {} if value is None else {"MAC_CREDENTIAL_RENEW_AT_FRACTION": value}
    assert configured_renew_fraction(env) == expected


@pytest.fixture
def isolated_mac_home(tmp_path, monkeypatch):
    home = tmp_path / ".mac"
    monkeypatch.setenv("MAC_HOME", str(home))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("MAC_CLIENT_PROFILES_DIR", raising=False)
    monkeypatch.delenv("MAC_CLIENT_CREDENTIALS_DIR", raising=False)
    return home


def _manifest(issued_at: str, expires_at: str, *, version: int = 1) -> dict:
    return {
        "schema": MANIFEST_SCHEMA,
        "client_id": "laptop",
        "display_name": "Laptop",
        "profile": "rocky",
        "fleet": "rocky",
        "connection": {"api_url": "https://mac.example.test", "mode": "direct"},
        "ssh": {},
        "credential": {
            "id": "laptop.v%d" % version,
            "token": TOKEN + str(version),
            "scopes": ["read", "write"],
            "issued_at": issued_at,
            "expires_at": expires_at,
        },
        "capabilities": [],
    }


def _install(manifest):
    from mac.client_profiles import install_enrollment_manifest

    return install_enrollment_manifest(manifest)


def test_profile_not_yet_due_is_a_no_op(isolated_mac_home):
    _install(_manifest("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))

    def runner(argv):  # pragma: no cover - must never be called
        raise AssertionError("renewal ran for a profile that was not due")

    result = renew_profile("rocky", now=datetime(2026, 9, 10, tzinfo=timezone.utc), runner=runner)
    assert result["status"] == "not_due"


def test_due_profile_renews_over_ssh_and_installs(isolated_mac_home):
    from mac.client_profiles import load_profile

    _install(_manifest("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
    renewed = _manifest("2026-09-16T00:00:00+00:00", "2026-10-16T00:00:00+00:00", version=2)
    calls = []

    class Result:
        returncode = 0
        stdout = json.dumps(renewed)
        stderr = ""

    def runner(argv):
        calls.append(argv)
        return Result()

    result = renew_profile(
        "rocky",
        now=datetime(2026, 9, 16, tzinfo=timezone.utc),
        runner=runner,
        fleets_config=str(_fleets_config(isolated_mac_home)),
    )

    assert result["status"] == "renewed"
    assert result["expires_at"].startswith("2026-10-16")
    assert result["previous_expires_at"].startswith("2026-10-01")
    # Renewal goes over SSH, invoking the hub-local command -- not an API route.
    assert calls and calls[0][0] == "ssh"
    assert "mac admin client renew laptop" in " ".join(calls[0])
    # The new credential is what the profile now presents.
    assert load_profile("rocky", include_token=True)["credential"]["token"].endswith("2")


def test_renewal_never_returns_the_token(isolated_mac_home):
    _install(_manifest("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))
    renewed = _manifest("2026-09-16T00:00:00+00:00", "2026-10-16T00:00:00+00:00", version=2)

    class Result:
        returncode = 0
        stdout = json.dumps(renewed)
        stderr = ""

    result = renew_profile(
        "rocky",
        now=datetime(2026, 9, 16, tzinfo=timezone.utc),
        runner=lambda argv: Result(),
        fleets_config=str(_fleets_config(isolated_mac_home)),
    )
    assert TOKEN + "2" not in json.dumps(result)


def test_failed_renewal_is_reported_while_the_old_credential_still_works(isolated_mac_home):
    _install(_manifest("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))

    class Result:
        returncode = 255
        stdout = ""
        stderr = "ssh: connect to host hub port 22: Connection refused"

    report = renew_due_profiles(
        now=datetime(2026, 9, 16, tzinfo=timezone.utc),
        runner=lambda argv: Result(),
        fleets_config=str(_fleets_config(isolated_mac_home)),
    )
    entry = next(p for p in report["profiles"] if p["profile"] == "rocky")
    assert entry["status"] == "error"
    assert "Connection refused" in entry["reason"]
    # Still inside the credential's life: the fleet keeps working while the
    # operator has runway to fix the renewal path.
    assert renewal_status(
        {"issued_at": "2026-09-01T00:00:00+00:00", "expires_at": "2026-10-01T00:00:00+00:00"},
        now=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )["expired"] is False


def test_one_profile_failing_does_not_suppress_another(isolated_mac_home):
    _install(_manifest("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))

    report = renew_due_profiles(
        profile_names=["rocky", "does-not-exist"],
        dry_run=True,
        now=datetime(2026, 9, 16, tzinfo=timezone.utc),
    )
    statuses = {p["profile"]: p["status"] for p in report["profiles"]}
    assert statuses["rocky"] == "would_renew"
    assert statuses["does-not-exist"] == "error"


def test_dry_run_moves_no_secret(isolated_mac_home):
    _install(_manifest("2026-09-01T00:00:00+00:00", "2026-10-01T00:00:00+00:00"))

    def runner(argv):  # pragma: no cover - a dry run must not reach SSH
        raise AssertionError("dry run attempted a renewal")

    result = renew_profile(
        "rocky", now=datetime(2026, 9, 16, tzinfo=timezone.utc), dry_run=True, runner=runner
    )
    assert result["status"] == "would_renew"


def _fleets_config(mac_home: Path) -> Path:
    """Minimal fleets.yaml so the hub's SSH route resolves.

    fleets.yaml is the definitive source of a fleet's hub coordinates, so
    renewal resolves its route there rather than recording a target in the
    client profile.
    """
    path = mac_home / "fleets.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "version: 1\n"
        "fleets:\n"
        "  rocky:\n"
        "    fleet_name: rocky\n"
        "    hub_agent: hub\n"
        "    hub_url: http://hub.example.test:8789\n"
        "    agents:\n"
        "    - name: hub\n"
        "      enabled: true\n"
        "      target: operator@hub.example.test\n"
        "      os: linux\n",
        encoding="utf-8",
    )
    return path
