#!/usr/bin/env python3
"""Materialize per-agent OpenClaw-only channel credentials from MAC's vault.

Telegram long polling permits one active gateway per bot token, so the durable
namespace is deliberately agent-specific::

    telegram.<agent>.bot
    telegram.<agent>.canary_target

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
    base = (os.environ.get("MAC_SECRET_VAULT_URL") or "").strip()
    vault_token = (os.environ.get("MAC_SECRET_VAULT_TOKEN") or "").strip()
    output_path = Path(
        os.environ.get("MAC_OPENCLAW_CREDENTIALS_FILE")
        or Path.home() / ".mac" / "openclaw" / "credentials.env"
    ).expanduser()
    if not agent or not base or not vault_token:
        print(
            "MAC_AGENT_NAME, MAC_SECRET_VAULT_URL, and MAC_SECRET_VAULT_TOKEN are required",
            file=sys.stderr,
        )
        return 2

    names = vault_list(base, vault_token)
    bot_name = "telegram.%s.bot" % agent
    target_name = "telegram.%s.canary_target" % agent
    if bot_name not in names:
        update_env_file(
            output_path,
            {
                "TELEGRAM_BOT_TOKEN": None,
                "MAC_OPENCLAW_TELEGRAM_CANARY_TARGET": None,
            },
        )
        print("no Telegram bot credential for agent=%s" % agent)
        return 0

    bot_token = vault_get(base, vault_token, bot_name)
    ok, identity = telegram_auth_test(bot_token)
    if not ok:
        print(
            "Telegram credential validation failed for agent=%s" % agent,
            file=sys.stderr,
        )
        return 4
    target = vault_get(base, vault_token, target_name) if target_name in names else ""
    changed = update_env_file(
        output_path,
        {
            "TELEGRAM_BOT_TOKEN": bot_token,
            "MAC_OPENCLAW_TELEGRAM_CANARY_TARGET": target or None,
        },
    )
    print(
        json.dumps(
            {
                "agent": agent,
                "bot_id": identity.get("id"),
                "bot_username": identity.get("username"),
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
