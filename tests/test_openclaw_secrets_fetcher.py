"""Secret-safe tests for the OpenClaw-only Telegram credential fetcher."""

from __future__ import annotations

import importlib.util
from pathlib import Path


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


def test_fetcher_uses_per_agent_vault_namespace_and_separate_openclaw_file() -> None:
    text = FETCHER.read_text(encoding="utf-8")
    assert '"telegram.%s.bot" % agent' in text
    assert '"telegram.%s.canary_target" % agent' in text
    assert '".mac" / "openclaw" / "credentials.env"' in text
    assert '".hermes"' not in text
