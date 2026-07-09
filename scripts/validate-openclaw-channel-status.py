#!/usr/bin/env python3
"""Validate the secret-free result of ``openclaw channels status --probe``."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping


REQUIRED_CHANNELS = ("slack", "telegram")


def channel_problems(
    payload: Mapping[str, Any], required_channels: tuple[str, ...] = REQUIRED_CHANNELS
) -> list[str]:
    accounts_by_channel = payload.get("channelAccounts") or {}
    default_accounts = payload.get("channelDefaultAccountId") or {}
    problems = []
    for channel in required_channels:
        accounts = accounts_by_channel.get(channel) or []
        configured = [
            account
            for account in accounts
            if isinstance(account, dict)
            and account.get("enabled") is True
            and account.get("configured") is True
        ]
        # Each MAC channel identity has one durable gateway owner. Multiple
        # active accounts can consume the same Socket Mode or long-poll stream
        # and make probes look healthy while real replies fail or disappear.
        if len(configured) != 1:
            problems.append(channel)
            continue
        account = configured[0]
        probe = account.get("probe")
        account_id = account.get("accountId")
        default_id = default_accounts.get(channel)
        if (
            not isinstance(probe, dict)
            or probe.get("ok") is not True
            or account.get("lastError")
            or (default_id is not None and account_id is not None and default_id != account_id)
        ):
            problems.append(channel)
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) not in {1, 3} or (len(args) == 3 and args[1] != "--required"):
        print(
            "usage: validate-openclaw-channel-status.py STATUS.json [--required slack,telegram]",
            file=sys.stderr,
        )
        return 2
    payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    required = (
        tuple(item.strip() for item in args[2].split(",") if item.strip())
        if len(args) == 3
        else REQUIRED_CHANNELS
    )
    problems = channel_problems(payload, required)
    if problems:
        print(
            "channel probe did not prove one healthy configured account: "
            + ", ".join(problems),
            file=sys.stderr,
        )
        return 1
    if required:
        print(
            "OpenClaw channel probes: "
            + " ".join("%s=healthy" % item for item in required)
        )
    else:
        print("OpenClaw channel probes: none configured (headless runtime)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
