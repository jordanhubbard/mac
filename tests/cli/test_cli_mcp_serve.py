"""`mac mcp serve` at the CLI layer.

The module-level behaviour is covered by tests/test_mcp_server.py. This is the
layer a coding agent actually launches -- `mac admin mcp serve`, handed to it
in an --mcp-config -- and a server that works as a module but not as a
subcommand is a server nothing can start.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.test_support import dsn_for


def _run(tmp_path, *args, stdin=""):
    """Run `mac --db <tmp> <args>` with a scripted stdin, returning stdout."""
    out = io.StringIO()
    old_out, old_in = sys.stdout, sys.stdin
    sys.stdout, sys.stdin = out, io.StringIO(stdin)
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    except SystemExit as exc:  # serve() exits with its return code
        rc = exc.code or 0
    finally:
        sys.stdout, sys.stdin = old_out, old_in
    return rc, out.getvalue()


def test_mcp_serve_exits_cleanly_on_closed_stdin(tmp_path):
    """The agent closing the pipe is a normal end, not a failure."""
    rc, out = _run(tmp_path, "admin", "mcp", "serve")

    assert rc == 0
    assert out == ""


def test_mcp_serve_answers_a_handshake(tmp_path):
    """The real thing: initialize, then list the tools, over stdio."""
    stdin = (
        "\n".join(
            json.dumps(m)
            for m in (
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
        )
        + "\n"
    )

    rc, out = _run(tmp_path, "admin", "mcp", "serve", stdin=stdin)

    assert rc == 0
    responses = [json.loads(line) for line in out.splitlines() if line.strip()]
    # Two, not three: the notification must not be answered.
    assert [r["id"] for r in responses] == [1, 2]
    assert responses[0]["result"]["serverInfo"]["name"] == "mac"
    assert {t["name"] for t in responses[1]["result"]["tools"]} == {
        "mac_task_show",
        "mac_task_list",
        "mac_task_ready",
        "mac_task_create",
    }


def test_mcp_serve_reaches_the_real_control_plane(tmp_path):
    """A tool call must go through the resolved plane, not a stub."""
    created = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "mac_task_create",
                "arguments": {"title": "filed by a coding agent", "project": "mac"},
            },
        }
    )

    rc, out = _run(tmp_path, "admin", "mcp", "serve", stdin=created + "\n")

    assert rc == 0
    payload = json.loads(json.loads(out.strip())["result"]["content"][0]["text"])
    assert payload["title"] == "filed by a coding agent"
    assert payload["id"].startswith("task_")
