#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional


TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}
STALE_HERMES_HOME_ENV_KEYS = {"SLACK_HOME_CHANNEL", "SLACK_HOME_CHANNEL_NAME"}


def _env_enabled(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in TRUTHY:
        return True
    if value in FALSY:
        return False
    return default


def _home_channel_name() -> str:
    return (
        os.environ.get("MAC_HERMES_SLACK_HOME_CHANNEL_NAME")
        or os.environ.get("ACC_SLACK_HOME_CHANNEL_NAME")
        or os.environ.get("SLACK_HOME_CHANNEL_NAME")
        or ""
    ).strip().lstrip("#")


def _normalize_workspace(name: str) -> str:
    name = name.strip().lower().replace("_", "-")
    return "offtera" if name == "ofterra" else name


def _load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    try:
        tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp_path.chmod(0o600)
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _normalize_legacy_hermes_env(path: Path, report: Dict[str, Any]) -> None:
    """Remove direct Slack home targets from Hermes env files.

    MAC uses slack_home_channels.json for multi-workspace routing. Leaving a
    legacy SLACK_HOME_CHANNEL in ~/.hermes/.env gives upstream Hermes a single
    concrete home target and makes lifecycle notices visible in Slack on only
    that host.
    """
    try:
        original = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        report["legacy_env_normalization"] = "missing"
        return

    removed: List[str] = []
    kept: List[str] = []
    for line in original.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key in STALE_HERMES_HOME_ENV_KEYS:
                removed.append(key)
                continue
        kept.append(line)

    if not removed:
        report["legacy_env_normalization"] = "unchanged"
        return

    tmp_path = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    try:
        tmp_path.write_text("\n".join(kept).rstrip() + "\n", encoding="utf-8")
        tmp_path.chmod(0o600)
        tmp_path.replace(path)
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
    report["legacy_env_normalization"] = "removed"
    report["legacy_env_removed_keys"] = sorted(set(removed))


def _load_accounts(path: Path) -> List[Dict[str, str]]:
    data = _load_json(path, [])
    if not isinstance(data, list):
        raise ValueError("%s is not a list" % path)
    accounts: List[Dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        name = _normalize_workspace(str(item.get("name") or ""))
        bot_token = str(item.get("bot_token") or "")
        if not name or not bot_token:
            continue
        accounts.append({"name": name, "bot_token": bot_token})
    return accounts


def _validate_home_entries(raw: Any) -> List[Dict[str, str]]:
    if not isinstance(raw, list):
        raise ValueError("home-channel JSON must be a list")
    entries: List[Dict[str, str]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        team_id = str(item.get("team_id") or "").strip()
        channel_id = str(item.get("channel_id") or item.get("chat_id") or "").strip()
        if not team_id or not channel_id:
            continue
        channel_name = str(item.get("channel_name") or channel_id).strip()
        entries.append(
            {
                "name": _normalize_workspace(str(item.get("name") or "")),
                "team_id": team_id,
                "channel_id": channel_id,
                "channel_name": channel_name,
            }
        )
    return entries


def _slack_api(bot_token: str, method: str, params: Dict[str, str]) -> Dict[str, Any]:
    base = os.environ.get("MAC_HERMES_SLACK_API_BASE", "https://slack.com/api").rstrip("/")
    data = urllib.parse.urlencode(params).encode("utf-8")
    request = urllib.request.Request(
        "%s/%s" % (base, method),
        data=data,
        headers={
            "Authorization": "Bearer %s" % bot_token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        body = json.loads(response.read().decode("utf-8"))
    if not body.get("ok"):
        raise RuntimeError("%s failed: %s" % (method, body.get("error") or body))
    return body


def _discover_home_channels(accounts: List[Dict[str, str]], channel_name: str) -> List[Dict[str, str]]:
    wanted = channel_name.lower()
    homes: List[Dict[str, str]] = []
    for account in accounts:
        auth = _slack_api(account["bot_token"], "auth.test", {})
        team_id = str(auth.get("team_id") or "")
        cursor = ""
        found: Optional[Dict[str, Any]] = None
        for _ in range(20):
            page = _slack_api(
                account["bot_token"],
                "conversations.list",
                {
                    "types": "public_channel,private_channel",
                    "exclude_archived": "true",
                    "limit": "200",
                    "cursor": cursor,
                },
            )
            for channel in page.get("channels", []):
                if str(channel.get("name") or "").lower() == wanted:
                    found = channel
                    break
            if found:
                break
            cursor = str(page.get("response_metadata", {}).get("next_cursor") or "")
            if not cursor:
                break
        if not found:
            raise RuntimeError("#%s not found for %s" % (channel_name, account["name"]))
        homes.append(
            {
                "name": account["name"],
                "team_id": team_id,
                "channel_id": str(found.get("id") or ""),
                "channel_name": "#%s" % channel_name,
            }
        )
    return [home for home in homes if home["team_id"] and home["channel_id"]]


def _write_home_data(home_path: Path, route_path: Path, homes: List[Dict[str, str]]) -> None:
    _write_json(home_path, homes)
    routes = _load_json(route_path, {})
    if not isinstance(routes, dict):
        routes = {}
    for home in homes:
        routes[str(home["channel_id"])] = str(home["team_id"])
    _write_json(route_path, routes)


def main(argv: List[str]) -> int:
    if len(argv) != 4:
        print(
            "usage: sync-hermes-home-channels.py ACCOUNTS HOME_CHANNELS CHANNEL_TEAMS REPORT",
            file=sys.stderr,
        )
        return 2
    accounts_path = Path(argv[0]).expanduser()
    home_path = Path(argv[1]).expanduser()
    route_path = Path(argv[2]).expanduser()
    report_path = Path(argv[3]).expanduser()
    channel_name = _home_channel_name()
    report: Dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "accounts_file": str(accounts_path),
        "home_channels_file": str(home_path),
        "channel_teams_file": str(route_path),
        "configured_home_channel_name": channel_name,
        "status": "unknown",
        "workspaces": [],
        "warnings": [],
    }
    _normalize_legacy_hermes_env(accounts_path.parent / ".env", report)

    if not _env_enabled("MAC_HERMES_SYNC_SLACK_HOME_CHANNELS", True) or not _env_enabled(
        "ACC_HERMES_SYNC_SLACK_HOME_CHANNELS", True
    ):
        report["status"] = "disabled"
        _write_json(report_path, report)
        return 0

    try:
        static_json = os.environ.get("MAC_HERMES_SLACK_HOME_CHANNELS_JSON")
        if static_json:
            homes = _validate_home_entries(json.loads(static_json))
        else:
            accounts = _load_accounts(accounts_path)
            report["workspaces"] = [account["name"] for account in accounts]
            if not accounts:
                report["status"] = "skipped_no_accounts"
                _write_json(report_path, report)
                return 0
            if not channel_name:
                report["status"] = "skipped_no_home_channel"
                _write_json(report_path, report)
                return 0
            homes = _discover_home_channels(accounts, channel_name)
        if not homes:
            report["status"] = "skipped_no_home_channels"
            _write_json(report_path, report)
            return 0
        _write_home_data(home_path, route_path, homes)
        report["status"] = "written"
        report["home_channel_count"] = len(homes)
        report["workspaces"] = [home.get("name") or home["team_id"] for home in homes]
        _write_json(report_path, report)
        return 0
    except Exception as exc:
        report["status"] = "preserved_existing"
        report["warnings"].append(str(exc))
        _write_json(report_path, report)
        print("[hermes-home-channels] WARNING: %s" % exc, file=sys.stderr)
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
