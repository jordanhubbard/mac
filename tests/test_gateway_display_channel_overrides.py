"""Per-channel display overrides (display.channels.<platform:chat_id>.*).

The gateway display resolver historically stopped at platform granularity:
`/reasoning show` in one Slack channel enabled reasoning display for EVERY
Slack channel. The channel tier lets one room opt into seeing the agent's
working/reasoning output without changing the platform default — the
consent model is "this channel asked for it", so the override must beat
platform and global settings, and a preference set on a channel must also
cover its threads (parent-channel fallback).
"""
from __future__ import annotations

import sys
from pathlib import Path

HERMES = Path(__file__).resolve().parents[1] / "src" / "mac" / "_hermes"
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))

from gateway.display_config import channel_config_key, resolve_display_setting


def test_channel_override_beats_platform_and_global():
    cfg = {
        "display": {
            "show_reasoning": False,
            "platforms": {"slack": {"show_reasoning": False}},
            "channels": {"slack:C123": {"show_reasoning": True}},
        }
    }
    assert resolve_display_setting(
        cfg, "slack", "show_reasoning", channel_key="slack:C123"
    ) is True


def test_channel_without_setting_falls_back_to_platform():
    cfg = {
        "display": {
            "platforms": {"slack": {"show_reasoning": True}},
            "channels": {"slack:C123": {"tool_progress": "all"}},
        }
    }
    assert resolve_display_setting(
        cfg, "slack", "show_reasoning", channel_key="slack:C123"
    ) is True


def test_unknown_channel_uses_platform_tier_default():
    # Slack's built-in default is show_reasoning off; an unrelated channel
    # override must not leak into other channels.
    cfg = {"display": {"channels": {"slack:C123": {"show_reasoning": True}}}}
    assert resolve_display_setting(
        cfg, "slack", "show_reasoning", channel_key="slack:C999"
    ) is False


def test_thread_falls_back_to_parent_channel_key():
    # Candidate keys are checked in order: exact chat (thread) first, then
    # the parent channel, so a channel-level opt-in covers its threads.
    cfg = {"display": {"channels": {"slack:C123": {"show_reasoning": True}}}}
    keys = ["slack:C123.thread.99", "slack:C123"]
    assert resolve_display_setting(
        cfg, "slack", "show_reasoning", channel_key=keys
    ) is True


def test_first_matching_channel_key_wins():
    cfg = {
        "display": {
            "channels": {
                "slack:T99": {"show_reasoning": False},
                "slack:C123": {"show_reasoning": True},
            }
        }
    }
    keys = ["slack:T99", "slack:C123"]
    assert resolve_display_setting(
        cfg, "slack", "show_reasoning", channel_key=keys
    ) is False


def test_channel_values_are_normalised():
    # YAML string forms behave like the platform tier ("on" -> True,
    # bare off -> False for tool_progress -> "off").
    cfg = {
        "display": {
            "channels": {
                "telegram:-100555": {
                    "show_reasoning": "on",
                    "tool_progress": False,
                }
            }
        }
    }
    key = channel_config_key("telegram", -100555)
    assert key == "telegram:-100555"
    assert resolve_display_setting(
        cfg, "telegram", "show_reasoning", channel_key=key
    ) is True
    assert resolve_display_setting(
        cfg, "telegram", "tool_progress", channel_key=key
    ) == "off"


def test_no_channel_key_preserves_legacy_resolution():
    cfg = {
        "display": {
            "channels": {"slack:C123": {"show_reasoning": True}},
            "platforms": {"slack": {"show_reasoning": False}},
        }
    }
    # Callers that don't pass channel_key are untouched by the new tier.
    assert resolve_display_setting(cfg, "slack", "show_reasoning") is False


def test_malformed_channels_section_is_ignored():
    for bad in ("not-a-dict", ["slack:C123"], 7):
        cfg = {"display": {"channels": bad}}
        assert resolve_display_setting(
            cfg, "slack", "show_reasoning", channel_key="slack:C123"
        ) is False
    cfg = {"display": {"channels": {"slack:C123": "not-a-dict"}}}
    assert resolve_display_setting(
        cfg, "slack", "show_reasoning", channel_key="slack:C123"
    ) is False
