#!/usr/bin/env python3
"""Validate the secret-free result of ``openclaw channels status``.

Exit codes:
  0 success
  1 retryable (gateway still starting, or a live probe has not come up yet)
  2 usage
  3 fatal (auth/config will not heal by waiting)
"""

from __future__ import annotations

import json
import re
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence


REQUIRED_CHANNELS = ("slack", "telegram")
RETRY = 1
USAGE = 2
FATAL = 3
_FATAL_ERROR = re.compile(
    r"invalid_auth|not_authed|token_revoked|account_inactive|"
    r"missing_scope|invalid_token|invalid_app_token",
    re.I,
)


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


def _configured_accounts(payload: Mapping[str, Any], channel: str) -> list[dict[str, Any]]:
    accounts_by_channel = payload.get("channelAccounts") or {}
    accounts = accounts_by_channel.get(channel) or []
    return [
        account
        for account in accounts
        if isinstance(account, dict)
        and account.get("enabled") is True
        and account.get("configured") is True
    ]


def _account_probe_ok(account: Mapping[str, Any]) -> bool:
    probe = account.get("probe")
    return isinstance(probe, dict) and probe.get("ok") is True and not account.get("lastError")


def _identity_id(account: Mapping[str, Any], channel: str) -> str | None:
    probe = account.get("probe")
    if not isinstance(probe, dict):
        return None
    identity = probe.get("team") if channel == "slack" else probe.get("bot")
    if isinstance(identity, dict) and identity.get("id"):
        return str(identity["id"])
    return None


def channel_problems(
    payload: Mapping[str, Any], required_channels: tuple[str, ...] = REQUIRED_CHANNELS
) -> list[str]:
    problems = []
    if payload.get("gatewayReachable") is False:
        problems.append("gateway")
    default_accounts = payload.get("channelDefaultAccountId") or {}
    for channel in required_channels:
        configured = _configured_accounts(payload, channel)
        if not configured:
            problems.append(channel)
            continue
        account_ids = {account.get("accountId") for account in configured}
        default_id = default_accounts.get(channel)
        identities = [
            identity
            for identity in (_identity_id(account, channel) for account in configured)
            if identity
        ]
        healthy = all(_account_probe_ok(account) for account in configured)
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


def classify_probe(
    payload: Mapping[str, Any],
    required_channels: Sequence[str] = REQUIRED_CHANNELS,
) -> tuple[str, list[str]]:
    """Return ``ok``, ``retry``, or ``fatal`` plus the failing names."""

    required = tuple(item for item in required_channels if item)
    problems = channel_problems(payload, required)
    if not problems:
        return "ok", []
    if "gateway" in problems:
        return "retry", problems
    default_accounts = payload.get("channelDefaultAccountId") or {}
    for channel in problems:
        configured = _configured_accounts(payload, channel)
        if not configured:
            # Config may still be loading during sandbox warmup.
            return "retry", problems
        account_ids = [account.get("accountId") for account in configured]
        if len(set(account_ids)) != len(configured):
            return "fatal", problems
        identities = [
            identity
            for identity in (_identity_id(account, channel) for account in configured)
            if identity
        ]
        if identities and len(identities) != len(set(identities)):
            return "fatal", problems
        default_id = default_accounts.get(channel)
        if default_id is not None and default_id not in set(account_ids):
            return "fatal", problems
        for account in configured:
            error = str(account.get("lastError") or "")
            if error and _FATAL_ERROR.search(error):
                return "fatal", problems
    return "retry", problems


def _parse_args(argv: list[str]) -> tuple[Path, tuple[str, ...], bool]:
    quiet = False
    required: tuple[str, ...] | None = None
    path: str | None = None
    rest = list(argv)
    while rest:
        item = rest.pop(0)
        if item == "--quiet":
            quiet = True
            continue
        if item == "--required":
            if not rest:
                raise ValueError("usage")
            required = tuple(part.strip() for part in rest.pop(0).split(",") if part.strip())
            continue
        if path is not None:
            raise ValueError("usage")
        path = item
    if path is None:
        raise ValueError("usage")
    return Path(path), REQUIRED_CHANNELS if required is None else required, quiet


def main(argv: list[str] | None = None) -> int:
    try:
        status_path, required, quiet = _parse_args(list(argv or sys.argv[1:]))
    except ValueError:
        print(
            "usage: validate-openclaw-channel-status.py STATUS.json "
            "[--required slack,telegram] [--quiet]",
            file=sys.stderr,
        )
        return USAGE
    try:
        payload = load_status_payload(status_path)
    except (OSError, ValueError) as exc:
        print("invalid OpenClaw channel status: %s" % exc, file=sys.stderr)
        return RETRY
    verdict, problems = classify_probe(payload, required)
    if verdict != "ok":
        if not quiet:
            print(
                "channel probe did not prove one healthy configured account: "
                + ", ".join(problems),
                file=sys.stderr,
            )
            print("probe_verdict=%s" % verdict, file=sys.stderr)
        return FATAL if verdict == "fatal" else RETRY
    if not quiet:
        if required:
            print("OpenClaw channel probes: " + " ".join("%s=healthy" % item for item in required))
        else:
            print("OpenClaw channel probes: none configured (headless runtime)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
