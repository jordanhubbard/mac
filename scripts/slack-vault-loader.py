#!/usr/bin/env python3
"""Verify Slack tokens locally, then upload the validated ones to
TokenHub's vault under stable key names.

Key naming: slack.<agent>.<workspace>.<kind>
  agent     ∈ {rocky, natasha, bullwinkle, ...}
  workspace ∈ {omgjkh, offtera, ...}  (Slack team slug, no .slack.com)
  kind      ∈ {bot, app, signing, client, uauth}

For bot and uauth tokens we run Slack auth.test before uploading;
anything else is shape-validated only (xoxb-/xoxp-/xapp-/8e2... etc).
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.parse
from pathlib import Path

SLACK_APPS_DIR = Path(os.environ.get("SLACK_APPS_DIR") or "~/Documents/Slack-Apps").expanduser()
TOKENHUB_URL = os.environ.get("TOKENHUB_URL", "http://100.125.137.89:8090").rstrip("/")
TOKENHUB_ADMIN_TOKEN = os.environ["TOKENHUB_ADMIN_TOKEN"]

# Tokens that are pre-verifiable via Slack auth.test
AUTH_TEST_KINDS = {"bot", "uauth"}

# Token prefix guards (skip files that don't look like the right kind of secret)
TOKEN_PREFIXES = {
    "bot": "xoxb-",
    "app": "xapp-",
    "uauth": "xoxp-",
    # signing-secret and client-secret are hex-ish; just non-empty.
}


def auth_test(token: str) -> dict:
    req = urllib.request.Request(
        "https://slack.com/api/auth.test",
        method="POST",
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=b"",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def apps_connections_open(token: str) -> dict:
    """App-level tokens (xapp-) are valid only for Socket Mode; verify
    by opening a WSS URL — Slack returns ok=false / invalid_auth if
    the app token has been rotated or the app's Socket Mode is off."""
    req = urllib.request.Request(
        "https://slack.com/api/apps.connections.open",
        method="POST",
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/x-www-form-urlencoded",
        },
        data=b"",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def vault_put(key: str, value: str) -> dict:
    safe_key = urllib.parse.quote(key, safe="")
    req = urllib.request.Request(
        "%s/admin/v1/vault/secrets/%s" % (TOKENHUB_URL, safe_key),
        method="PUT",
        headers={
            "Authorization": "Bearer %s" % TOKENHUB_ADMIN_TOKEN,
            "Content-Type": "application/json",
        },
        data=json.dumps({"value": value}).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def vault_list() -> list[str]:
    req = urllib.request.Request(
        "%s/admin/v1/vault/secrets" % TOKENHUB_URL,
        headers={"Authorization": "Bearer %s" % TOKENHUB_ADMIN_TOKEN},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read()).get("secrets") or []


def main() -> int:
    if not SLACK_APPS_DIR.is_dir():
        print("Slack-Apps dir not found:", SLACK_APPS_DIR, file=sys.stderr)
        return 2

    report = []
    seen_token_values = {}  # canonicalize: same token contents -> store once
    file_pattern = re.compile(r"^(?P<agent>[A-Za-z0-9_-]+)-(?P<kind>bot|app|signing|client|uauth)(?:-token|-secret)?\.txt$")

    for workspace_dir in sorted(SLACK_APPS_DIR.iterdir()):
        if not workspace_dir.is_dir():
            continue
        workspace = workspace_dir.name
        if workspace.endswith(".slack.com"):
            workspace = workspace[: -len(".slack.com")]
        for f in sorted(workspace_dir.iterdir()):
            m = file_pattern.match(f.name)
            if not m:
                continue
            agent = m.group("agent")
            kind = m.group("kind")
            value = f.read_text(encoding="utf-8").strip()
            if not value:
                report.append({"file": str(f), "skipped": "empty"})
                continue
            prefix = TOKEN_PREFIXES.get(kind)
            if prefix and not value.startswith(prefix):
                report.append({"file": str(f), "skipped": "wrong_prefix"})
                continue

            key = "slack.%s.%s.%s" % (agent, workspace, kind)
            verification = None
            if kind in AUTH_TEST_KINDS:
                try:
                    result = auth_test(value)
                except Exception as exc:
                    verification = {"status": "auth_test_error", "error": str(exc)}
                else:
                    if not result.get("ok"):
                        verification = {
                            "status": "auth_test_failed",
                            "error": result.get("error"),
                            "team": result.get("team"),
                            "user": result.get("user"),
                        }
                    else:
                        verification = {
                            "status": "ok",
                            "team": result.get("team"),
                            "user": result.get("user"),
                            "team_id": result.get("team_id"),
                        }
                if verification.get("status") != "ok":
                    report.append({"file": str(f), "key": key, "verify": verification})
                    continue
            elif kind == "app":
                try:
                    result = apps_connections_open(value)
                except Exception as exc:
                    verification = {"status": "apps_connections_open_error", "error": str(exc)}
                else:
                    if not result.get("ok"):
                        verification = {
                            "status": "apps_connections_open_failed",
                            "error": result.get("error"),
                        }
                    else:
                        verification = {"status": "ok"}
                if verification.get("status") != "ok":
                    report.append({"file": str(f), "key": key, "verify": verification})
                    continue

            # De-duplicate identical token values across files (some agents
            # have the same token copied into multiple workspace dirs).
            existing_key = seen_token_values.get(value)
            try:
                resp = vault_put(key, value)
            except Exception as exc:
                report.append({"file": str(f), "key": key, "verify": verification, "put_error": str(exc)})
                continue
            seen_token_values[value] = key
            report.append({
                "file": str(f),
                "key": key,
                "verify": verification,
                "stored": resp.get("ok") is True,
                "duplicate_of": existing_key,
            })

    print(json.dumps({"report": report, "vault_secrets": vault_list()}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
