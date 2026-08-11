"""`mac admin human-interface coverage` — what a switch would actually carry.

"Is the migration solid?" cannot be answered by a port summary that counts
files: it reports two ported and says nothing about whether the third exists.
This command names every artefact and whether it arrives, so the decision to
switch is made from evidence rather than from a success message.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _profile(home, interface, files):
    from mac.human_interface_profile import layout_for

    layout = layout_for(interface, home)
    layout.identity_dir.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (layout.identity_dir / name).write_text(body, encoding="utf-8")


def test_coverage_reports_solid_when_everything_arrives(tmp_path):
    files = {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "m"}
    _profile(tmp_path, "openclaw", files)
    _profile(tmp_path, "hermes", files)

    rc, out = _run(
        tmp_path, "admin", "human-interface", "coverage",
        "--from", "openclaw", "--to", "hermes", "--home", str(tmp_path),
    )

    assert rc in (None, 0)
    assert out["solid"] is True


def test_coverage_names_what_would_be_left_behind(tmp_path):
    """The answer that matters. An artefact missing at the target is named, not
    omitted -- a silent omission reads as "nothing to do"."""
    _profile(tmp_path, "openclaw", {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "m"})
    _profile(tmp_path, "hermes", {"SOUL.md": "s"})

    rc, out = _run(
        tmp_path, "admin", "human-interface", "coverage",
        "--from", "openclaw", "--to", "hermes", "--home", str(tmp_path),
    )

    assert rc in (None, 0)
    assert out["solid"] is False
    assert "MEMORY.md" in out["unresolved"]


def test_coverage_reports_telegram_alongside_slack(tmp_path):
    """Telegram was absent from the port entirely, so a switch moved Slack and
    dropped Telegram while reporting success."""
    _profile(tmp_path, "openclaw", {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "m"})
    _profile(tmp_path, "hermes", {"SOUL.md": "s", "USER.md": "u", "MEMORY.md": "m"})

    _rc, out = _run(
        tmp_path, "admin", "human-interface", "coverage",
        "--from", "openclaw", "--to", "hermes", "--home", str(tmp_path),
    )

    named = {item["artefact"] for item in out["items"]}
    assert "telegram accounts" in named
    assert "slack accounts" in named
