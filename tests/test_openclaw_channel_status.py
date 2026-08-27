"""Behavioral tests for configured-only OpenClaw channel probe validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path


VALIDATOR = Path(__file__).resolve().parents[1] / "scripts" / "validate-openclaw-channel-status.py"


def load_validator():
    spec = importlib.util.spec_from_file_location("validate_openclaw_channel_status", VALIDATOR)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def account(
    *, account_id: str = "default", team_id: str = "T-default", ok: bool = True, error: str = ""
) -> dict[str, object]:
    result: dict[str, object] = {
        "accountId": account_id,
        "enabled": True,
        "configured": True,
        "probe": {"ok": ok, "team": {"id": team_id}},
    }
    if error:
        result["lastError"] = error
    return result


def test_default_compatibility_mode_requires_both_healthy_channels() -> None:
    validator = load_validator()
    payload = {
        "channelAccounts": {
            "slack": [account()],
            "telegram": [account()],
        }
    }
    assert validator.channel_problems(payload) == []

    payload["channelAccounts"].pop("telegram")
    assert validator.channel_problems(payload) == ["telegram"]


def test_probe_failure_or_runtime_error_fails_closed() -> None:
    validator = load_validator()
    payload = {
        "channelAccounts": {
            "slack": [account(ok=False)],
            "telegram": [account(error="polling conflict")],
        }
    }
    assert validator.channel_problems(payload) == ["slack", "telegram"]


def test_headless_runtime_has_no_required_channel_probe() -> None:
    validator = load_validator()
    assert validator.channel_problems({}, ()) == []


def test_explicitly_unreachable_gateway_fails_even_for_headless_runtime(tmp_path) -> None:
    validator = load_validator()
    status = tmp_path / "status.txt"
    status.write_text(
        "Gateway not reachable: connection closed\n"
        '{"gatewayReachable": false, "channelAccounts": {}}\n',
        encoding="utf-8",
    )

    payload = validator.load_status_payload(status)
    assert validator.channel_problems(payload, ()) == ["gateway"]
    assert validator.classify_probe(payload, ()) == ("retry", ["gateway"])


def test_auth_error_is_fatal_and_duplicate_accounts_are_fatal() -> None:
    validator = load_validator()
    auth = {
        "channelAccounts": {
            "slack": [account(ok=False, error="invalid_auth")],
        }
    }
    assert validator.classify_probe(auth, ("slack",)) == ("fatal", ["slack"])
    duplicates = {
        "channelAccounts": {
            "slack": [
                account(account_id="default", team_id="T-offtera"),
                account(account_id="offtera", team_id="T-offtera"),
            ],
        },
        "channelDefaultAccountId": {"slack": "default"},
    }
    assert validator.classify_probe(duplicates, ("slack",)) == ("fatal", ["slack"])


def test_probe_not_ok_without_auth_error_is_retryable() -> None:
    validator = load_validator()
    payload = {"channelAccounts": {"slack": [account(ok=False)]}}
    assert validator.classify_probe(payload, ("slack",)) == ("retry", ["slack"])


def test_quiet_retry_hides_human_line(tmp_path) -> None:
    import subprocess
    import sys

    status = tmp_path / "status.json"
    status.write_text('{"gatewayReachable": false, "channelAccounts": {}}\n', encoding="utf-8")
    quiet = subprocess.run(
        [sys.executable, str(VALIDATOR), "--quiet", "--required", "", str(status)],
        capture_output=True,
        text=True,
        check=False,
    )
    loud = subprocess.run(
        [sys.executable, str(VALIDATOR), "--required", "", str(status)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert quiet.returncode == 1
    assert quiet.stderr == ""
    assert loud.returncode == 1
    assert "channel probe did not prove" in loud.stderr
    assert "probe_verdict=retry" in loud.stderr


def test_single_configured_channel_is_validated_without_requiring_others() -> None:
    validator = load_validator()
    payload = {"channelAccounts": {"slack": [account()]}}
    assert validator.channel_problems(payload, ("slack",)) == []
    payload["channelAccounts"]["slack"] = [account(ok=False)]
    assert validator.channel_problems(payload, ("slack",)) == ["slack"]


def test_duplicate_active_accounts_fail_even_when_both_probes_are_healthy() -> None:
    validator = load_validator()
    payload = {
        "channelAccounts": {
            "slack": [
                account(account_id="default", team_id="T-offtera"),
                account(account_id="offtera", team_id="T-offtera"),
            ],
        },
        "channelDefaultAccountId": {"slack": "default"},
    }
    assert validator.channel_problems(payload, ("slack",)) == ["slack"]


def test_default_account_must_name_a_configured_account() -> None:
    validator = load_validator()
    payload = {
        "channelAccounts": {"slack": [account(account_id="offtera")]},
        "channelDefaultAccountId": {"slack": "default"},
    }
    assert validator.channel_problems(payload, ("slack",)) == ["slack"]
    payload["channelDefaultAccountId"]["slack"] = "offtera"
    assert validator.channel_problems(payload, ("slack",)) == []


def test_distinct_slack_workspaces_are_valid_native_multi_account_residency() -> None:
    validator = load_validator()
    payload = {
        "channelAccounts": {
            "slack": [
                account(account_id="offtera", team_id="T-offtera"),
                account(account_id="omgjkh", team_id="T-omgjkh"),
            ],
        },
        "channelDefaultAccountId": {"slack": "offtera"},
    }
    assert validator.channel_problems(payload, ("slack",)) == []
