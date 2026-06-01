#!/usr/bin/env python3
"""Fetch this agent's Slack secrets from the TokenHub vault and apply
them to the local Hermes config files (~/.hermes/config.yaml +
~/.hermes/slack_accounts.json).

Run at deploy time (and any time tokens are rotated in the vault) so
each agent's hermes-gateway has the right credentials without having
to scatter secrets across host-specific .env files.

Key namespace: slack.<agent>.<workspace>.<kind>
  agent: this host's agent name (rocky / natasha / bullwinkle)
  workspace: Slack team slug (omgjkh, offtera)
  kind: bot, app, signing, client, uauth

Environment:
  MAC_AGENT_NAME         (required)  this agent's short name
  TOKENHUB_URL           (default http://127.0.0.1:8090)
  TOKENHUB_ADMIN_TOKEN   (required)  vault admin token
  HERMES_HOME            (default ~/.hermes)
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def vault_list(base: str, token: str) -> list[str]:
    req = urllib.request.Request(
        "%s/admin/v1/vault/secrets" % base.rstrip("/"),
        headers={"Authorization": "Bearer %s" % token},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("secrets") or []


def vault_get(base: str, token: str, key: str) -> str:
    safe = urllib.parse.quote(key, safe="")
    req = urllib.request.Request(
        "%s/admin/v1/vault/secrets/%s" % (base.rstrip("/"), safe),
        headers={"Authorization": "Bearer %s" % token},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("value") or ""


def auth_test(token: str) -> tuple[bool, dict]:
    """Optional last-mile verification before applying. Returns
    (ok, response). Network failures count as ok=False so we don't
    write a known-bad value, but we still allow opt-out via
    MAC_SKIP_SLACK_VERIFY=1 for offline / locked-down redeploys."""
    if os.environ.get("MAC_SKIP_SLACK_VERIFY", "").lower() in {"1", "true", "yes"}:
        return True, {"skipped": True}
    req = urllib.request.Request(
        "https://slack.com/api/auth.test",
        method="POST",
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=b"",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read())
    except (urllib.error.URLError, OSError) as exc:
        return False, {"error": str(exc)}
    return bool(payload.get("ok")), payload


def update_yaml_env_block(config_path: Path, kvs: dict[str, str]) -> bool:
    """In-place update of the top-level ``env:`` block in
    ``~/.hermes/config.yaml``. Adds keys that are missing and updates
    keys that exist. Returns True if the file was modified.
    """
    text = config_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    out: list[str] = []
    in_env = False
    env_indent: str | None = None
    handled: set[str] = set()
    changed = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if not in_env and line.rstrip() == "env:":
            in_env = True
            out.append(line)
            i += 1
            continue
        if in_env:
            if line.strip() == "":
                out.append(line)
                i += 1
                continue
            # detect end of env block: next top-level (zero-indent non-comment) key
            if line[:1] not in {" ", "\t", "#"} and line.strip().endswith(":"):
                # flush new keys before leaving block
                indent = env_indent or "  "
                for k, v in kvs.items():
                    if k not in handled:
                        out.append("%s%s: %s" % (indent, k, v))
                        handled.add(k)
                        changed = True
                in_env = False
                out.append(line)
                i += 1
                continue
            stripped = line.lstrip()
            indent = line[: len(line) - len(stripped)]
            if env_indent is None and indent:
                env_indent = indent
            for k, v in kvs.items():
                prefix = "%s:" % k
                if stripped.startswith(prefix):
                    new_line = "%s%s: %s" % (indent, k, v)
                    if new_line != line:
                        out.append(new_line)
                        changed = True
                    else:
                        out.append(line)
                    handled.add(k)
                    break
            else:
                out.append(line)
            i += 1
            continue
        out.append(line)
        i += 1

    if in_env:
        indent = env_indent or "  "
        for k, v in kvs.items():
            if k not in handled:
                out.append("%s%s: %s" % (indent, k, v))
                handled.add(k)
                changed = True
    elif "env:" not in text:
        out.append("env:")
        indent = "  "
        for k, v in kvs.items():
            out.append("%s%s: %s" % (indent, k, v))
            handled.add(k)
        changed = True
    if changed:
        config_path.write_text("\n".join(out) + ("\n" if not text.endswith("\n") else ""), encoding="utf-8")
    return changed


def write_slack_accounts(hermes_home: Path, accounts: list[dict]) -> None:
    path = hermes_home / "slack_accounts.json"
    path.write_text(json.dumps(accounts, indent=2) + "\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def upsert_env_file(env_path: Path, kvs: dict[str, str]) -> bool:
    """Idempotently set KEY=VALUE lines in a .env file, preserving everything
    else. Returns True if the file changed.

    This is the durable fix for the gateway coming up "No messaging platforms
    enabled" after a restart: the gateway wrapper sources ~/.hermes/.env, so the
    Slack tokens must live THERE (not only in config.yaml's env: block, which a
    freshly systemd-launched gateway doesn't reliably load before deciding which
    platforms to enable).
    """
    if not kvs:
        return False
    existing = ""
    if env_path.exists():
        existing = env_path.read_text(encoding="utf-8", errors="ignore")
    lines = existing.splitlines()
    out: list[str] = []
    handled: set[str] = set()
    changed = False
    for line in lines:
        key = line.split("=", 1)[0].strip() if ("=" in line and not line.lstrip().startswith("#")) else None
        if key in kvs:
            new_line = "%s=%s" % (key, kvs[key])
            if new_line != line:
                changed = True
            out.append(new_line)
            handled.add(key)
        else:
            out.append(line)
    for key, value in kvs.items():
        if key not in handled:
            out.append("%s=%s" % (key, value))
            changed = True
    if changed:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
    return changed


def main() -> int:
    agent = (os.environ.get("MAC_AGENT_NAME") or "").strip().lower()
    if not agent:
        print("MAC_AGENT_NAME is required", file=sys.stderr)
        return 2
    base = os.environ.get("TOKENHUB_URL", "http://127.0.0.1:8090")
    token = os.environ.get("TOKENHUB_ADMIN_TOKEN", "")
    if not token:
        print("TOKENHUB_ADMIN_TOKEN is required", file=sys.stderr)
        return 2
    hermes_home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes").expanduser()
    hermes_home.mkdir(parents=True, exist_ok=True)
    config_path = hermes_home / "config.yaml"

    try:
        all_secrets = vault_list(base, token)
    except Exception as exc:
        print("vault_list failed:", exc, file=sys.stderr)
        return 3

    # Find secrets for this agent
    prefix = "slack.%s." % agent
    keys = sorted(k for k in all_secrets if k.startswith(prefix))
    if not keys:
        print("no slack secrets for agent=%s in vault" % agent, file=sys.stderr)
        return 0

    # Group by workspace -> kind -> value
    by_workspace: dict[str, dict[str, str]] = {}
    for key in keys:
        parts = key.split(".")
        if len(parts) < 4:
            continue
        _, _, workspace, kind = parts[0], parts[1], parts[2], parts[3]
        try:
            value = vault_get(base, token, key)
        except Exception as exc:
            print("vault_get %s failed: %s" % (key, exc), file=sys.stderr)
            continue
        if not value:
            continue
        by_workspace.setdefault(workspace, {})[kind] = value

    if not by_workspace:
        print("no usable slack secrets for agent=%s" % agent, file=sys.stderr)
        return 0

    # Pre-verify each workspace's bot token before writing. Dedupe by
    # Slack team_id — operators sometimes copy the same token into
    # multiple workspace-named directories, which would otherwise
    # produce a slack_accounts.json with two entries that both
    # actually talk to the same team. The "extra" entry would then
    # invalid_auth when used to post to a channel that doesn't exist
    # in its real team.
    accounts: list[dict] = []
    primary_bot = None
    primary_app = None
    summary = []
    seen_team_ids: dict[str, str] = {}  # team_id -> workspace key chosen
    for workspace, kinds in sorted(by_workspace.items()):
        bot = kinds.get("bot")
        app_tok = kinds.get("app")
        if not bot:
            summary.append({"workspace": workspace, "skipped": "no_bot_token"})
            continue
        ok, info = auth_test(bot)
        if not ok:
            summary.append({"workspace": workspace, "skipped": "auth_test_failed", "info": info})
            continue
        team_id = info.get("team_id") or ""
        actual_team = info.get("team") or workspace
        if team_id and team_id in seen_team_ids:
            summary.append({
                "workspace": workspace,
                "skipped": "duplicate_team",
                "team_id": team_id,
                "duplicate_of": seen_team_ids[team_id],
            })
            continue
        if team_id:
            seen_team_ids[team_id] = workspace
        # Use the team_id-derived name when it differs from the
        # operator's directory name, so downstream channel routing
        # matches what Slack actually reports.
        account_name = actual_team if actual_team else workspace
        account = {"name": account_name, "bot_token": bot}
        if app_tok:
            account["app_token"] = app_tok
        accounts.append(account)
        summary.append({
            "workspace": workspace,
            "stored": True,
            "team": actual_team,
            "team_id": team_id,
            "user": info.get("user"),
            "account_name": account_name,
        })
        if primary_bot is None:
            primary_bot = bot
            primary_app = app_tok

    if not accounts:
        print("no accounts passed verification for agent=%s" % agent, file=sys.stderr)
        return 4

    # Write slack_accounts.json (canonical multi-workspace store)
    write_slack_accounts(hermes_home, accounts)

    # Update config.yaml env block with primary bot/app tokens (single-workspace
    # consumers in Hermes still read these env vars).
    env_updates: dict[str, str] = {}
    if primary_bot:
        env_updates["SLACK_BOT_TOKEN"] = primary_bot
    if primary_app:
        env_updates["SLACK_APP_TOKEN"] = primary_app
    config_changed = False
    if config_path.exists() and env_updates:
        config_changed = update_yaml_env_block(config_path, env_updates)

    # Durable fix: also write the primary tokens into ~/.hermes/.env, which the
    # gateway wrapper sources at startup. Without this, a systemd-restarted
    # gateway has no SLACK_BOT_TOKEN/SLACK_APP_TOKEN in its process env and comes
    # up "No messaging platforms enabled" (Slack silent), even though the tokens
    # are present in config.yaml.
    env_file_changed = upsert_env_file(hermes_home / ".env", env_updates)

    print(json.dumps({
        "agent": agent,
        "workspaces": summary,
        "slack_accounts_path": str(hermes_home / "slack_accounts.json"),
        "config_yaml_path": str(config_path),
        "config_yaml_changed": config_changed,
        "env_file_changed": env_file_changed,
        "env_file_path": str(hermes_home / ".env"),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
