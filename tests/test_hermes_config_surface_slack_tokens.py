"""The config surface promotes slack_accounts.json tokens into config.yaml's env
block so a redeploy keeps the Slack platform enabled (the hostd case).

An agent provisioned via a multi-workspace slack_accounts.json carries its
bot/app tokens there, not in config.yaml's env: block — and the gateway enables
Slack off the env tokens. Without promotion a restarted gateway reports "No
messaging platforms enabled".
"""

from __future__ import annotations

import json

import yaml

import mac.hermes_config_surface as hcs


def _write_accounts(home, accounts):
    (home / "slack_accounts.json").write_text(json.dumps(accounts), encoding="utf-8")


def test_promotes_first_valid_account_tokens(tmp_path):
    _write_accounts(tmp_path, [
        {"name": "offtera", "bot_token": "xoxb-AAA", "app_token": "xapp-BBB", "user_token": "xoxp-CCC"},
        {"name": "omgagentuser", "bot_token": "xoxb-DDD", "app_token": "xapp-EEE"},
    ])
    cfg: dict = {}
    hcs._promote_slack_accounts_tokens(cfg, tmp_path)
    assert cfg["env"]["SLACK_BOT_TOKEN"] == "xoxb-AAA"
    assert cfg["env"]["SLACK_APP_TOKEN"] == "xapp-BBB"
    assert cfg["env"]["SLACK_USER_TOKEN"] == "xoxp-CCC"


def test_explicit_env_tokens_win(tmp_path):
    _write_accounts(tmp_path, [{"name": "x", "bot_token": "xoxb-AAA", "app_token": "xapp-BBB"}])
    cfg = {"env": {"SLACK_BOT_TOKEN": "xoxb-EXPLICIT", "SLACK_APP_TOKEN": "xapp-EXPLICIT"}}
    hcs._promote_slack_accounts_tokens(cfg, tmp_path)
    assert cfg["env"]["SLACK_BOT_TOKEN"] == "xoxb-EXPLICIT"  # not clobbered


def test_no_accounts_file_is_noop(tmp_path):
    cfg: dict = {}
    hcs._promote_slack_accounts_tokens(cfg, tmp_path)
    assert "env" not in cfg or not cfg["env"].get("SLACK_BOT_TOKEN")


def test_skips_accounts_without_valid_token_shape(tmp_path):
    _write_accounts(tmp_path, [{"name": "bad", "bot_token": "not-a-token", "app_token": ""}])
    cfg: dict = {}
    hcs._promote_slack_accounts_tokens(cfg, tmp_path)
    assert not cfg.get("env", {}).get("SLACK_BOT_TOKEN")


def test_apply_payload_promotes_tokens_end_to_end(tmp_path):
    (tmp_path / "config.yaml").write_text("{}\n")
    _write_accounts(tmp_path, [{"name": "offtera", "bot_token": "xoxb-LIVE", "app_token": "xapp-LIVE"}])
    hcs.apply_hermes_surface_payload({}, target_home=tmp_path)
    written = yaml.safe_load((tmp_path / "config.yaml").read_text()) or {}
    assert (written.get("env") or {}).get("SLACK_BOT_TOKEN") == "xoxb-LIVE"
    assert (written.get("env") or {}).get("SLACK_APP_TOKEN") == "xapp-LIVE"
