"""Tests for the Slack-secret fetcher's .env upsert (the gateway-Slack fix).

A systemd-restarted gateway came up "No messaging platforms enabled" because
the Slack tokens lived only in config.yaml's env: block, not in ~/.hermes/.env
(which the gateway wrapper sources). The fetcher now writes the primary tokens
into ~/.hermes/.env; these tests lock that behavior in.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_FETCHER = Path(__file__).resolve().parent.parent / "scripts" / "mac-fetch-slack-secrets.py"


def _load():
    spec = importlib.util.spec_from_file_location("mac_fetch_slack_secrets", _FETCHER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(not _FETCHER.exists(), reason="fetcher script not present")
def test_upsert_adds_tokens_preserving_other_lines(tmp_path):
    m = _load()
    env = tmp_path / ".env"
    env.write_text("TOKENHUB_URL=http://hub:8090\n# comment\nOPENAI_API_KEY=x\n")
    changed = m.upsert_env_file(env, {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1"})
    assert changed is True
    text = env.read_text()
    assert "SLACK_BOT_TOKEN=xoxb-1" in text
    assert "SLACK_APP_TOKEN=xapp-1" in text
    assert "TOKENHUB_URL=http://hub:8090" in text  # preserved
    assert "# comment" in text                      # comment preserved
    assert "OPENAI_API_KEY=x" in text


@pytest.mark.skipif(not _FETCHER.exists(), reason="fetcher script not present")
def test_upsert_is_idempotent_and_updates_in_place(tmp_path):
    m = _load()
    env = tmp_path / ".env"
    m.upsert_env_file(env, {"SLACK_BOT_TOKEN": "xoxb-1"})
    # same value → no change
    assert m.upsert_env_file(env, {"SLACK_BOT_TOKEN": "xoxb-1"}) is False
    # new value → in-place update, no duplicate line
    assert m.upsert_env_file(env, {"SLACK_BOT_TOKEN": "xoxb-2"}) is True
    text = env.read_text()
    assert text.count("SLACK_BOT_TOKEN=") == 1
    assert "xoxb-2" in text and "xoxb-1" not in text


@pytest.mark.skipif(not _FETCHER.exists(), reason="fetcher script not present")
def test_upsert_creates_file_and_noop_on_empty(tmp_path):
    m = _load()
    env = tmp_path / "sub" / ".env"  # parent doesn't exist yet
    assert m.upsert_env_file(env, {}) is False         # nothing to write
    assert not env.exists()
    assert m.upsert_env_file(env, {"SLACK_BOT_TOKEN": "xoxb-9"}) is True
    assert env.exists() and "xoxb-9" in env.read_text()
