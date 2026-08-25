"""`mac admin plugin` at the CLI layer (ADR 0023)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

from mac.cli import main
from mac.test_support import dsn_for

REPO = Path(__file__).resolve().parents[2]


def _run(tmp_path, *args, env_home=None):
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


def _skills(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    skill = root / "mac-cli"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# mac-cli\n", encoding="utf-8")
    return root


def test_plugin_install_status_uninstall(tmp_path, monkeypatch):
    mac_home = tmp_path / "mac-home"
    user_home = tmp_path / "user"
    (user_home / ".cursor").mkdir(parents=True)
    monkeypatch.setenv("MAC_HOME", str(mac_home))
    skills = _skills(tmp_path)

    rc, installed, _err = _run(
        tmp_path,
        "admin",
        "plugin",
        "install",
        "--scope",
        "global",
        "--user-home",
        str(user_home),
        "--skills-root",
        str(skills),
    )
    assert rc in (None, 0)
    assert installed["harnesses"]["cursor"] == "installed"
    assert (mac_home / "plugin" / "plugin.json").is_file()

    rc, reported, _err = _run(tmp_path, "admin", "plugin", "status")
    assert rc in (None, 0)
    assert reported["installed"] is True

    rc, removed, _err = _run(
        tmp_path,
        "admin",
        "plugin",
        "uninstall",
        "--user-home",
        str(user_home),
    )
    assert rc in (None, 0)
    assert removed["removed"] is True
    assert not (mac_home / "plugin").exists()


def test_plugin_install_refuses_the_mac_source_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_HOME", str(tmp_path / "mac-home"))
    rc, _out, err = _run(
        tmp_path,
        "admin",
        "plugin",
        "install",
        "--scope",
        "repo",
        "--repo",
        str(REPO),
        "--skills-root",
        str(_skills(tmp_path)),
    )
    assert rc == 1
    assert "mac source tree" in err
