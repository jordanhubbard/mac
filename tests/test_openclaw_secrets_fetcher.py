"""Secret-safe tests for the OpenClaw-only Telegram credential fetcher."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import subprocess


FETCHER = Path(__file__).resolve().parents[1] / "scripts" / "mac-fetch-openclaw-secrets.py"


def load_fetcher():
    spec = importlib.util.spec_from_file_location("mac_fetch_openclaw_secrets", FETCHER)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_owner_only_env_update_is_idempotent_and_supports_revocation(tmp_path: Path) -> None:
    module = load_fetcher()
    path = tmp_path / "credentials.env"
    token = "123456:test-value"
    assert module.update_env_file(
        path,
        {
            "TELEGRAM_BOT_TOKEN": token,
            "MAC_OPENCLAW_TELEGRAM_CANARY_TARGET": "42",
        },
    )
    assert path.stat().st_mode & 0o777 == 0o600
    assert not module.update_env_file(
        path,
        {
            "TELEGRAM_BOT_TOKEN": token,
            "MAC_OPENCLAW_TELEGRAM_CANARY_TARGET": "42",
        },
    )
    assert module.update_env_file(
        path,
        {
            "TELEGRAM_BOT_TOKEN": None,
            "MAC_OPENCLAW_TELEGRAM_CANARY_TARGET": None,
        },
    )
    assert "TELEGRAM" not in path.read_text(encoding="utf-8")


def test_fetcher_prefers_logical_identity_namespace_with_legacy_agent_fallback() -> None:
    text = FETCHER.read_text(encoding="utf-8")
    assert '"channel-identity.%s.telegram.%s.bot"' in text
    assert 'canonical_prefix = "channel-identity.%s.slack."' in text
    assert '"%s%s.bot" % (canonical_prefix, account)' in text
    assert '"%s%s.app" % (canonical_prefix, account)' in text
    assert '"telegram.%s.bot" % agent' in text
    assert '"telegram.%s.canary_target" % agent' in text
    assert '".mac" / "openclaw" / "credentials.env"' in text
    assert '".hermes"' not in text


def test_discovers_all_complete_slack_workspaces_with_primary_first() -> None:
    module = load_fetcher()
    names = [
        "slack.bullwinkle.omgjkh.bot",
        "slack.bullwinkle.omgjkh.app",
        "slack.bullwinkle.offtera.bot",
        "slack.bullwinkle.offtera.app",
    ]
    assert module.discover_slack_account_secrets(names, "bullwinkle", "bullwinkle", "offtera") == [
        (
            "offtera",
            "slack.bullwinkle.offtera.bot",
            "slack.bullwinkle.offtera.app",
        ),
        (
            "omgjkh",
            "slack.bullwinkle.omgjkh.bot",
            "slack.bullwinkle.omgjkh.app",
        ),
    ]


def test_headless_runtime_clears_credentials_without_vault_access(tmp_path: Path) -> None:
    credentials = tmp_path / "credentials.env"
    credentials.write_text(
        "SLACK_BOT_TOKEN=xoxb-stale\nSLACK_APP_TOKEN=xapp-stale\nTELEGRAM_BOT_TOKEN=123:stale\n",
        encoding="utf-8",
    )
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "MAC_AGENT_NAME": "headless-worker",
        "MAC_OPENCLAW_CREDENTIALS_FILE": str(credentials),
    }

    result = subprocess.run(
        [str(FETCHER)],
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )

    assert result.returncode == 0
    assert "headless" in result.stdout
    assert credentials.read_text(encoding="utf-8") == ""
