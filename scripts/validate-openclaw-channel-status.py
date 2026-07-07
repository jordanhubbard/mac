#!/usr/bin/env python3
"""Validate the secret-free result of ``openclaw channels status --probe``."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping


REQUIRED_CHANNELS = ("slack", "telegram")


def channel_problems(payload: Mapping[str, Any]) -> list[str]:
    accounts_by_channel = payload.get("channelAccounts") or {}
    problems = []
    for channel in REQUIRED_CHANNELS:
        accounts = accounts_by_channel.get(channel) or []
        healthy = False
        for account in accounts:
            probe = account.get("probe") if isinstance(account, dict) else None
            if (
                isinstance(account, dict)
                and account.get("enabled") is True
                and account.get("configured") is True
                and isinstance(probe, dict)
                and probe.get("ok") is True
                and not account.get("lastError")
            ):
                healthy = True
                break
        if not healthy:
            problems.append(channel)
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(argv or sys.argv[1:])
    if len(args) != 1:
        print("usage: validate-openclaw-channel-status.py STATUS.json", file=sys.stderr)
        return 2
    payload = json.loads(Path(args[0]).read_text(encoding="utf-8"))
    problems = channel_problems(payload)
    if problems:
        print(
            "channel probe did not prove healthy configured account(s): "
            + ", ".join(problems),
            file=sys.stderr,
        )
        return 1
    print("OpenClaw channel probes: slack=healthy telegram=healthy")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
