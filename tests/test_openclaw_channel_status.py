"""Behavioral tests for configured-only OpenClaw channel probe validation."""

from __future__ import annotations

import importlib.util
from pathlib import Path


VALIDATOR = (
    Path(__file__).resolve().parents[1]
    / "scripts"
    / "validate-openclaw-channel-status.py"
)


def load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_openclaw_channel_status", VALIDATOR
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def account(*, ok: bool = True, error: str = "") -> dict[str, object]:
    result: dict[str, object] = {
        "enabled": True,
        "configured": True,
        "probe": {"ok": ok},
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


def test_single_configured_channel_is_validated_without_requiring_others() -> None:
    validator = load_validator()
    payload = {"channelAccounts": {"slack": [account()]}}
    assert validator.channel_problems(payload, ("slack",)) == []
    payload["channelAccounts"]["slack"] = [account(ok=False)]
    assert validator.channel_problems(payload, ("slack",)) == ["slack"]
