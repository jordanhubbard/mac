#!/usr/bin/env python3
"""Materialize identity-scoped OpenClaw channel credentials from MAC's vault.

Workers without ``MAC_OPENCLAW_PUBLIC_IDENTITY`` are headless and receive no
human-channel credentials.  A logical identity account uses names such as::

    channel-identity.mac-hive.slack.default.bot
    channel-identity.mac-hive.slack.default.app
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
    return [
        str(row.get("name"))
        for row in rows
        if isinstance(row, dict) and row.get("name")
    ]


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
    lines = (
        path.read_text(encoding="utf-8", errors="ignore").splitlines()
        if path.exists()
        else []
    )
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


def main() -> int:
    agent = (os.environ.get("MAC_AGENT_NAME") or "").strip().lower()
    identity = (os.environ.get("MAC_OPENCLAW_PUBLIC_IDENTITY") or "").strip().lower()
    slack_account = (os.environ.get("MAC_OPENCLAW_SLACK_ACCOUNT_ID") or "default").strip().lower()
    telegram_account = (
        os.environ.get("MAC_OPENCLAW_TELEGRAM_ACCOUNT_ID") or "default"
    ).strip().lower()
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
        update_env_file(
            output_path,
            {
                "SLACK_BOT_TOKEN": None,
                "SLACK_APP_TOKEN": None,
                "TELEGRAM_BOT_TOKEN": None,
                "MAC_OPENCLAW_TELEGRAM_CANARY_TARGET": None,
            },
        )
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
        "channel-identity.%s.telegram.%s.canary_target"
        % (identity, telegram_account),
        "telegram.%s.canary_target" % agent,
    )
    slack_bot_name = first_name(
        "channel-identity.%s.slack.%s.bot" % (identity, slack_account),
        "slack.%s.%s.bot" % (agent, slack_account),
    )
    slack_app_name = first_name(
        "channel-identity.%s.slack.%s.app" % (identity, slack_account),
        "slack.%s.%s.app" % (agent, slack_account),
    )

    telegram_token = (
        vault_get(base, vault_token, telegram_bot_name) if telegram_bot_name else ""
    )
    telegram_identity: dict[str, object] = {}
    if telegram_token:
        ok, telegram_identity = telegram_auth_test(telegram_token)
        if not ok:
            print(
                "Telegram credential validation failed for identity=%s" % identity,
                file=sys.stderr,
            )
            return 4
    target = (
        vault_get(base, vault_token, telegram_target_name)
        if telegram_target_name
        else ""
    )
    slack_bot = vault_get(base, vault_token, slack_bot_name) if slack_bot_name else ""
    slack_app = vault_get(base, vault_token, slack_app_name) if slack_app_name else ""
    if bool(slack_bot) != bool(slack_app):
        print(
            "Slack identity account requires both bot and app credentials",
            file=sys.stderr,
        )
        return 4
    if slack_bot and not slack_bot.startswith("xoxb-"):
        print("Slack bot credential has the wrong type", file=sys.stderr)
        return 4
    if slack_app and not slack_app.startswith("xapp-"):
        print("Slack app credential has the wrong type", file=sys.stderr)
        return 4
    if not slack_bot and not telegram_token:
        print(
            "no channel credentials found for public identity=%s" % identity,
            file=sys.stderr,
        )
        return 3
    changed = update_env_file(
        output_path,
        {
            "SLACK_BOT_TOKEN": slack_bot or None,
            "SLACK_APP_TOKEN": slack_app or None,
            "TELEGRAM_BOT_TOKEN": telegram_token or None,
            "MAC_OPENCLAW_TELEGRAM_CANARY_TARGET": target or None,
        },
    )
    print(
        json.dumps(
            {
                "agent": agent,
                "public_identity": identity,
                "channels": [
                    channel
                    for channel, configured in (
                        ("slack", bool(slack_bot)),
                        ("telegram", bool(telegram_token)),
                    )
                    if configured
                ],
                "telegram_bot_id": telegram_identity.get("id"),
                "telegram_bot_username": telegram_identity.get("username"),
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
