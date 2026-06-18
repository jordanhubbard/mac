"""The mac deploy retires the legacy <PLATFORM>_HOME_CHANNEL env var and instead
resolves the fleet's configured home channel into slack_home_channels.json, which
the gateway applies to config.home_channel. The "no home channel" prompt must
therefore key off the resolved/config home channel, not just the legacy env var
(see gateway/run.py). This guards the consumption path that populates it."""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERMES = Path(__file__).resolve().parents[1] / "src" / "mac" / "_hermes"
if str(HERMES) not in sys.path:
    sys.path.insert(0, str(HERMES))


def test_slack_home_resolved_from_file(tmp_path, monkeypatch):
    from gateway.config import _slack_home_from_resolved_file

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "slack_home_channels.json").write_text(
        json.dumps(
            [
                {
                    "channel_id": "C0AMSBEU7CJ",
                    "channel_name": "#rockyandfriends",
                    "name": "offtera",
                    "team_id": "THJ9A47K3",
                }
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    chan, name = _slack_home_from_resolved_file()
    assert chan == "C0AMSBEU7CJ"
    assert name == "rockyandfriends"  # leading '#' stripped


def test_slack_home_absent_returns_empty(tmp_path, monkeypatch):
    from gateway.config import _slack_home_from_resolved_file

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    chan, name = _slack_home_from_resolved_file("fallback")
    assert chan == ""
    assert name == "fallback"


_TWO_WORKSPACES = [
    {"channel_id": "C0AMSBEU7CJ", "channel_name": "#rockyandfriends", "name": "offtera", "team_id": "THJ9A47K3"},
    {"channel_id": "C0AMNRSN9EZ", "channel_name": "#rockyandfriends", "name": "omgjkh", "team_id": "TE0V8MBEJ"},
]


def test_slack_homes_resolved_returns_every_workspace(tmp_path, monkeypatch):
    # slackhome-01: broadcast deliveries must reach all connected workspaces, not homes[0].
    from gateway.config import _slack_homes_from_resolved_file

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "slack_home_channels.json").write_text(json.dumps(_TWO_WORKSPACES), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    assert _slack_homes_from_resolved_file() == [
        ("C0AMSBEU7CJ", "rockyandfriends"),
        ("C0AMNRSN9EZ", "rockyandfriends"),
    ]


def test_get_home_channels_returns_one_per_slack_workspace(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig, Platform

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "slack_home_channels.json").write_text(json.dumps(_TWO_WORKSPACES), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    homes = GatewayConfig().get_home_channels(Platform.SLACK)
    assert [h.chat_id for h in homes] == ["C0AMSBEU7CJ", "C0AMNRSN9EZ"]
    assert all(h.platform == Platform.SLACK for h in homes)
