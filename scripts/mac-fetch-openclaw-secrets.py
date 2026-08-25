#!/usr/bin/env python3
"""Materialize identity-scoped OpenClaw channel credentials from MAC's vault.

Workers without ``MAC_OPENCLAW_PUBLIC_IDENTITY`` are headless and receive no
human-channel credentials.  A logical identity can own multiple accounts per
channel and uses names such as::

    channel-identity.mac-hive.slack.default.bot
    channel-identity.mac-hive.slack.default.app
    channel-identity.mac-hive.slack.second-workspace.bot
    channel-identity.mac-hive.slack.second-workspace.app
    channel-identity.mac-hive.telegram.default.bot
    channel-identity.mac-hive.telegram.default.canary_target

The bot token is validated with Telegram ``getMe`` before it is written.  The
owner-only output is separate from ``~/.hermes/.env`` so a retained rollback
Hermes service cannot start polling the same Telegram bot during cutover.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import urllib.error
import urllib.parse
import urllib.request


def vault_list(base: str, token: str) -> list[str]:
    request = urllib.request.Request(
        "%s/secrets" % base.rstrip("/"),
        headers={"Authorization": "Bearer %s" % token},
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = json.loads(response.read())
    rows = payload if isinstance(payload, list) else payload.get("items") or []
    return [str(row.get("name")) for row in rows if isinstance(row, dict) and row.get("name")]


def vault_get(base: str, token: str, name: str) -> str:
    encoded = urllib.parse.quote(name, safe="")
    request = urllib.request.Request(
        "%s/secrets/%s/resolve" % (base.rstrip("/"), encoded),
        method="POST",
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
        },
        data=b"{}",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return str(json.loads(response.read()).get("value") or "")


def telegram_auth_test(token: str) -> tuple[bool, dict[str, object]]:
    if os.environ.get("MAC_SKIP_TELEGRAM_VERIFY", "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return True, {"skipped": True}
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/getMe" % token,
        headers={"Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, ValueError) as exc:
        return False, {"error": exc.__class__.__name__}
    result = payload.get("result") if isinstance(payload, dict) else None
    return bool(payload.get("ok") and isinstance(result, dict)), result or {}


def update_env_file(path: Path, updates: dict[str, str | None]) -> bool:
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines() if path.exists() else []
    output: list[str] = []
    seen: set[str] = set()
    changed = False
    for line in lines:
        key = (
            line.split("=", 1)[0].strip()
            if "=" in line and not line.lstrip().startswith("#")
            else ""
        )
        if key not in updates:
            output.append(line)
            continue
        seen.add(key)
        value = updates[key]
        if value is None:
            changed = True
            continue
        replacement = "%s=%s" % (key, value)
        output.append(replacement)
        changed = changed or replacement != line
    for key, value in updates.items():
        if key not in seen and value is not None:
            output.append("%s=%s" % (key, value))
            changed = True
    if not changed:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(output) + ("\n" if output else ""), encoding="utf-8")
    path.chmod(0o600)
    return True


def env_suffix(account_id: str) -> str:
    value = "".join(char if char.isalnum() else "_" for char in account_id.upper())
    return value.strip("_") or "DEFAULT"


def slack_env_keys(account_id: str) -> tuple[str, str]:
    suffix = env_suffix(account_id)
    return (
        "MAC_OPENCLAW_SLACK_%s_BOT_TOKEN" % suffix,
        "MAC_OPENCLAW_SLACK_%s_APP_TOKEN" % suffix,
    )


def discover_slack_account_secrets(
    names: list[str], identity: str, agent: str, primary: str
) -> list[tuple[str, str, str]]:
    """Return complete Slack bot/app pairs, primary first.

    Identity-scoped names win over the legacy per-agent namespace.  Discovery
    keeps every complete workspace pair so OpenClaw can use its native
    multi-account support instead of silently dropping all but one workspace.
    """

    available = set(names)
    accounts: set[str] = set()
    canonical_prefix = "channel-identity.%s.slack." % identity
    legacy_prefix = "slack.%s." % agent
    for name in available:
        for prefix in (canonical_prefix, legacy_prefix):
            if not name.startswith(prefix):
                continue
            remainder = name[len(prefix) :]
            if remainder.endswith(".bot") or remainder.endswith(".app"):
                accounts.add(remainder.rsplit(".", 1)[0])
    ordered = ([primary] if primary in accounts else []) + sorted(accounts - {primary})
    result: list[tuple[str, str, str]] = []
    for account in ordered:
        canonical = (
            "%s%s.bot" % (canonical_prefix, account),
            "%s%s.app" % (canonical_prefix, account),
        )
        legacy = (
            "%s%s.bot" % (legacy_prefix, account),
            "%s%s.app" % (legacy_prefix, account),
        )
        if canonical[0] in available and canonical[1] in available:
            result.append((account, canonical[0], canonical[1]))
        elif legacy[0] in available and legacy[1] in available:
            result.append((account, legacy[0], legacy[1]))
    return result


def stale_channel_env_updates(path: Path) -> dict[str, None]:
    """Remove legacy and previously generated channel variables on rewrite."""

    stale = {
        "SLACK_BOT_TOKEN",
        "SLACK_APP_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "MAC_OPENCLAW_SLACK_ACCOUNT_ID",
        "MAC_OPENCLAW_SLACK_ACCOUNT_IDS",
        "MAC_OPENCLAW_TELEGRAM_BOT_TOKEN",
        "MAC_OPENCLAW_TELEGRAM_CANARY_TARGET",
    }
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key = line.split("=", 1)[0].strip() if "=" in line else ""
            if key.startswith("MAC_OPENCLAW_SLACK_") and key.endswith(("_BOT_TOKEN", "_APP_TOKEN")):
                stale.add(key)
    return {key: None for key in stale}


def main() -> int:
    agent = (os.environ.get("MAC_AGENT_NAME") or "").strip().lower()
    identity = (os.environ.get("MAC_OPENCLAW_PUBLIC_IDENTITY") or "").strip().lower()
    slack_account = (os.environ.get("MAC_OPENCLAW_SLACK_ACCOUNT_ID") or "default").strip().lower()
    telegram_account = (
        (os.environ.get("MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID") or "default").strip().lower()
    )
    base = (os.environ.get("MAC_SECRET_VAULT_URL") or "").strip()
    vault_token = (os.environ.get("MAC_SECRET_VAULT_TOKEN") or "").strip()
    output_path = Path(
        os.environ.get("MAC_OPENCLAW_CREDENTIALS_FILE")
        or Path.home() / ".mac" / "openclaw" / "credentials.env"
    ).expanduser()
    if not agent:
        print("MAC_AGENT_NAME is required", file=sys.stderr)
        return 2

    if not identity:
        update_env_file(output_path, stale_channel_env_updates(output_path))
        print("OpenClaw runtime is headless for agent=%s" % agent)
        return 0

    if not base or not vault_token:
        print(
            "public OpenClaw identities require MAC_SECRET_VAULT_URL and MAC_SECRET_VAULT_TOKEN",
            file=sys.stderr,
        )
        return 2

    names = vault_list(base, vault_token)

    def first_name(*candidates: str) -> str | None:
        return next((candidate for candidate in candidates if candidate in names), None)

    telegram_bot_name = first_name(
        "channel-identity.%s.telegram.%s.bot" % (identity, telegram_account),
        "telegram.%s.bot" % agent,
    )
    telegram_target_name = first_name(
        "channel-identity.%s.telegram.%s.canary_target" % (identity, telegram_account),
        "telegram.%s.canary_target" % agent,
    )
    slack_secret_names = discover_slack_account_secrets(names, identity, agent, slack_account)

    telegram_token = vault_get(base, vault_token, telegram_bot_name) if telegram_bot_name else ""
    telegram_identity: dict[str, object] = {}
    if telegram_token:
        ok, telegram_identity = telegram_auth_test(telegram_token)
        if not ok:
            print(
                "Telegram credential validation failed for identity=%s" % identity,
                file=sys.stderr,
            )
            return 4
    target = vault_get(base, vault_token, telegram_target_name) if telegram_target_name else ""
    slack_credentials: dict[str, tuple[str, str]] = {}
    for account, bot_name, app_name in slack_secret_names:
        bot = vault_get(base, vault_token, bot_name)
        app = vault_get(base, vault_token, app_name)
        if not bot.startswith("xoxb-"):
            print("Slack bot credential has the wrong type", file=sys.stderr)
            return 4
        if not app.startswith("xapp-"):
            print("Slack app credential has the wrong type", file=sys.stderr)
            return 4
        slack_credentials[account] = (bot, app)
    if not slack_credentials and not telegram_token:
        print(
            "no channel credentials found for public identity=%s" % identity,
            file=sys.stderr,
        )
        return 3
    updates: dict[str, str | None] = dict(stale_channel_env_updates(output_path))
    account_ids = list(slack_credentials)
    if account_ids:
        updates["MAC_OPENCLAW_SLACK_ACCOUNT_ID"] = account_ids[0]
        updates["MAC_OPENCLAW_SLACK_ACCOUNT_IDS"] = ",".join(account_ids)
    for account, (bot, app) in slack_credentials.items():
        bot_key, app_key = slack_env_keys(account)
        updates[bot_key] = bot
        updates[app_key] = app
    updates["MAC_OPENCLAW_TELEGRAM_BOT_TOKEN"] = telegram_token or None
    updates["MAC_OPENCLAW_TELEGRAM_CANARY_TARGET"] = target or None
    changed = update_env_file(output_path, updates)
    print(
        json.dumps(
            {
                "agent": agent,
                "public_identity": identity,
                "channels": [
                    channel
                    for channel, configured in (
                        ("slack", bool(slack_credentials)),
                        ("telegram", bool(telegram_token)),
                    )
                    if configured
                ],
                "telegram_bot_id": telegram_identity.get("id"),
                "telegram_bot_username": telegram_identity.get("username"),
                "slack_accounts": account_ids,
                "canary_target_configured": bool(target),
                "credentials_file": str(output_path),
                "changed": changed,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
