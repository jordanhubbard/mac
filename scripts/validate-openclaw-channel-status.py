#!/usr/bin/env python3
"""Validate the secret-free result of ``openclaw channels status --probe``."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Any, Mapping


REQUIRED_CHANNELS = ("slack", "telegram")


def load_status_payload(path: Path) -> Mapping[str, Any]:
    """Load the JSON object even when OpenClaw prefixes it with diagnostics.

    ``openclaw channels status --json`` writes human-readable gateway failure
    guidance before its JSON result on some failure paths. Treat that output as
    a structured failed probe instead of surfacing a misleading JSON decoder
    error or accepting an empty headless-channel set.
    """

    text = path.read_text(encoding="utf-8")
    decoder = json.JSONDecoder()
    for offset, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, Mapping):
            return value
    raise ValueError("OpenClaw channel status did not contain a JSON object")


def channel_problems(
    payload: Mapping[str, Any], required_channels: tuple[str, ...] = REQUIRED_CHANNELS
) -> list[str]:
    accounts_by_channel = payload.get("channelAccounts") or {}
    default_accounts = payload.get("channelDefaultAccountId") or {}
    problems = []
    if payload.get("gatewayReachable") is False:
        problems.append("gateway")
    for channel in required_channels:
        accounts = accounts_by_channel.get(channel) or []
        configured = [
            account
            for account in accounts
            if isinstance(account, dict)
            and account.get("enabled") is True
            and account.get("configured") is True
        ]
        if not configured:
            problems.append(channel)
            continue
        account_ids = {account.get("accountId") for account in configured}
        default_id = default_accounts.get(channel)
        identities: list[str] = []
        healthy = True
        for account in configured:
            probe = account.get("probe")
            if (
                not isinstance(probe, dict)
                or probe.get("ok") is not True
                or account.get("lastError")
            ):
                healthy = False
                break
            identity = probe.get("team") if channel == "slack" else probe.get("bot")
            if isinstance(identity, dict) and identity.get("id"):
                identities.append(str(identity["id"]))
        # Multi-workspace residency is native OpenClaw behavior. What is not
        # valid is two account names resolving to the same Slack team or bot,
        # which is how an implicit environment account hid the reply outage.
        if (
            not healthy
            or len(account_ids) != len(configured)
            or len(identities) != len(set(identities))
            or (default_id is not None and default_id not in account_ids)
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
    try:
        payload = load_status_payload(Path(args[0]))
    except (OSError, ValueError) as exc:
        print("invalid OpenClaw channel status: %s" % exc, file=sys.stderr)
        return 1
    required = (
        tuple(item.strip() for item in args[2].split(",") if item.strip())
        if len(args) == 3
        else REQUIRED_CHANNELS
    )
    problems = channel_problems(payload, required)
    if problems:
        print(
            "channel probe did not prove one healthy configured account: " + ", ".join(problems),
            file=sys.stderr,
        )
        return 1
    if required:
        print("OpenClaw channel probes: " + " ".join("%s=healthy" % item for item in required))
    else:
        print("OpenClaw channel probes: none configured (headless runtime)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
