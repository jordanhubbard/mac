"""`mac admin cli-session` at the CLI layer (ADR 0032)."""

from __future__ import annotations

import io
import json
import sys

from mac import cli_session
from mac.cli import main
from mac.test_support import dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None), err.getvalue()


def test_cli_session_ensure_registered_uses_isolated_hook_config(tmp_path, monkeypatch):
    settings = tmp_path / "user" / ".claude" / "settings.json"
    monkeypatch.setattr(cli_session, "_claude_settings_path", lambda user_home=None: settings)
    monkeypatch.setattr(
        cli_session,
        "_cache_path",
        lambda agent_id: tmp_path / "cache" / (agent_id + ".json"),
    )

    rc, identity, err = _run(
        tmp_path,
        "admin",
        "cli-session",
        "ensure-registered",
        "--harness",
        "claude",
    )

    assert rc in (None, 0), err
    assert identity["agent_id"].startswith("agent_cli_")
    document = json.loads(settings.read_text(encoding="utf-8"))
    assert document["hooks"]["UserPromptSubmit"][0]["hooks"][0][
        "command"
    ] == cli_session._hook_command("UserPromptSubmit")


def test_cli_session_hook_renders_the_selected_event(tmp_path):
    rc, output, err = _run(
        tmp_path,
        "admin",
        "cli-session",
        "hook",
        "--event",
        "SessionStart",
    )

    assert rc in (None, 0), err
    assert output == {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "",
        }
    }
