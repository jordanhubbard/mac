"""Public API of mac.hermes_home_audit stays stable and read-only."""

from __future__ import annotations

import os
from pathlib import Path

from mac.hermes_home_audit import (
    HERMES_HOME_AUDIT_SCHEMA,
    HERMES_KNOWN_TOP_LEVEL,
    audit_hermes_home,
    classify_named_children,
)


def test_schema_constant():
    assert HERMES_HOME_AUDIT_SCHEMA == "mac.hermes_home_audit.v1"
    assert len(HERMES_KNOWN_TOP_LEVEL) == 66


def test_audit_missing_home(tmp_path):
    missing = tmp_path / "nope"
    report = audit_hermes_home(missing)
    assert report["schema"] == HERMES_HOME_AUDIT_SCHEMA
    assert report["status"] == "missing"
    assert report["home_exists"] is False
    assert report["entries"] == []
    assert report["scripts"] == []


def test_audit_not_a_directory(tmp_path):
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    report = audit_hermes_home(home=target)
    assert report["status"] == "not_a_directory"


def test_audit_unreadable(tmp_path):
    home = tmp_path / "locked"
    home.mkdir()
    home.chmod(0)
    try:
        report = audit_hermes_home(home)
    finally:
        home.chmod(0o700)
    assert report["status"] == "unreadable"


def test_known_vs_unknown_top_level(tmp_path):
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "SOUL.md").write_text("s", encoding="utf-8")
    (home / "stray").write_text("x", encoding="utf-8")
    report = audit_hermes_home(home)
    names = {item["name"]: item["classification"] for item in report["entries"]}
    assert names["SOUL.md"] == "canonical"
    assert names["stray"] == "drift"
    assert report["summary"]["unknown_count"] == 1
    assert report["summary"]["known_count"] == 1
    assert report["unknown_top_level"][0]["name"] == "stray"


def test_default_home_uses_mac_paths(tmp_path, monkeypatch):
    gw = tmp_path / "gw"
    gw.mkdir()
    (gw / "config.yaml").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(gw))
    report = audit_hermes_home()
    assert report["home_path"] == str(gw)
    assert report["entries"][0]["name"] == "config.yaml"


def test_classify_named_children_helper(tmp_path):
    d = tmp_path / "c"
    d.mkdir()
    (d / "keep").write_text("1", encoding="utf-8")
    (d / "drop").write_text("2", encoding="utf-8")
    items = classify_named_children(d, {"keep"}, container="box")
    by_name = {item["name"]: item for item in items}
    assert by_name["keep"]["classification"] == "canonical"
    assert by_name["keep"]["path"] == "box/keep"
    assert by_name["drop"]["classification"] == "drift"


def test_audit_detects_py_script_in_scripts_dir(tmp_path):
    home = tmp_path / "h"
    scripts = home / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "dream_cycle.py").write_text("print(1)\n", encoding="utf-8")
    report = audit_hermes_home(home)
    assert report["summary"]["script_count"] == 1
    entry = report["scripts"][0]
    assert entry["name"] == "dream_cycle.py"
    assert entry["source"] == "scripts"
    assert entry["path"] == "scripts/dream_cycle.py"


def test_audit_detects_sh_script_in_bin(tmp_path):
    home = tmp_path / "h"
    bin_dir = home / "bin"
    bin_dir.mkdir(parents=True)
    script = bin_dir / "helper.sh"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    script.chmod(0o755)
    report = audit_hermes_home(home)
    entry = report["scripts"][0]
    assert entry["name"] == "helper.sh"
    assert entry["source"] == "bin"
    assert entry["executable"] is True


def test_audit_nonexecutable_py_still_included(tmp_path):
    home = tmp_path / "h"
    hooks = home / "hooks"
    hooks.mkdir(parents=True)
    py = hooks / "on_start.py"
    py.write_text("x = 1\n", encoding="utf-8")
    py.chmod(0o644)
    report = audit_hermes_home(home)
    entry = report["scripts"][0]
    assert entry["executable"] is False
    assert entry["source"] == "hooks"


def test_audit_script_count_in_summary(tmp_path):
    home = tmp_path / "h"
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "a.py").write_text("1\n", encoding="utf-8")
    (home / "scripts" / "b.py").write_text("2\n", encoding="utf-8")
    (home / "scripts" / "ignore.txt").write_text("nope\n", encoding="utf-8")
    report = audit_hermes_home(home)
    assert report["summary"]["script_count"] == 2


def test_audit_script_entry_has_expected_fields(tmp_path):
    home = tmp_path / "h"
    (home / "scripts").mkdir(parents=True)
    (home / "scripts" / "x.py").write_text("1\n", encoding="utf-8")
    entry = audit_hermes_home(home)["scripts"][0]
    for key in ("name", "path", "source", "executable", "kind"):
        assert key in entry


def test_audit_script_source_field_reflects_scan_dir(tmp_path):
    home = tmp_path / "h"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "z.py").write_text("1\n", encoding="utf-8")
    assert audit_hermes_home(home)["scripts"][0]["source"] == "bin"


def test_audit_script_source_hooks(tmp_path):
    home = tmp_path / "h"
    (home / "hooks").mkdir(parents=True)
    (home / "hooks" / "pre.py").write_text("1\n", encoding="utf-8")
    assert audit_hermes_home(home)["scripts"][0]["source"] == "hooks"


def test_read_only(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    (home / "SOUL.md").write_text("s", encoding="utf-8")
    before = []
    for dirpath, dirnames, filenames in os.walk(home):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = path.lstat()
            before.append((str(path.relative_to(home)), st.st_mtime_ns, st.st_mode))
    audit_hermes_home(home)
    after = []
    for dirpath, dirnames, filenames in os.walk(home):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            st = path.lstat()
            after.append((str(path.relative_to(home)), st.st_mtime_ns, st.st_mode))
    assert before == after


def test_no_home_literals():
    source = Path(__import__("mac.hermes_home_audit", fromlist=["x"]).__file__).read_text(
        encoding="utf-8"
    )
    assert "Path.home()" not in source
    assert '".hermes"' not in source
    assert "mac._hermes" not in source


def test_kind_symlink_fifo_and_inaccessible(tmp_path, monkeypatch):
    from mac import hermes_home_audit as hha

    home = tmp_path / "h"
    home.mkdir()
    target = tmp_path / "real"
    target.mkdir()
    (home / "SOUL.md").symlink_to(target)
    fifo = home / "weird"
    os.mkfifo(fifo)
    report = audit_hermes_home(home)
    kinds = {item["name"]: item["kind"] for item in report["entries"]}
    assert kinds["SOUL.md"] == "symlink"
    assert kinds["weird"] == "other"

    ghost = tmp_path / "missing-name"
    real_lstat = Path.lstat

    def boom(self):
        if self == ghost:
            raise OSError("gone")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", boom)
    assert hha._kind(ghost) == "inaccessible"


def test_exists_and_iterdir_oserror(tmp_path, monkeypatch):
    from mac import hermes_home_audit as hha

    real_exists = Path.exists

    def exist_boom(self):
        raise OSError("x")

    monkeypatch.setattr(Path, "exists", exist_boom)
    assert hha._exists(tmp_path) is False
    monkeypatch.setattr(Path, "exists", real_exists)

    real_iterdir = Path.iterdir

    def iter_boom(self):
        raise OSError("list fail")

    monkeypatch.setattr(Path, "iterdir", iter_boom)
    assert hha.safe_iterdir(tmp_path) == []
    monkeypatch.setattr(Path, "iterdir", real_iterdir)


def test_access_oserror_and_dangling_script_dir(tmp_path, monkeypatch):
    from mac import hermes_home_audit as hha

    home = tmp_path / "h"
    home.mkdir()
    (home / "scripts").symlink_to(tmp_path / "missing-scripts")
    report = audit_hermes_home(home)
    assert report["scripts"] == []

    home2 = tmp_path / "h2"
    scripts = home2 / "scripts"
    scripts.mkdir(parents=True)
    py = scripts / "a.py"
    py.write_text("1\n", encoding="utf-8")

    def access_boom(path, mode):
        raise OSError("access")

    monkeypatch.setattr(os, "access", access_boom)
    report = audit_hermes_home(home2)
    assert report["scripts"][0]["executable"] is False


def test_is_dir_oserror_status(tmp_path, monkeypatch):
    home = tmp_path / "h"
    home.mkdir()
    real_is_dir = Path.is_dir

    def boom(self):
        if self == home:
            raise OSError("x")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", boom)
    assert audit_hermes_home(home)["status"] == "unreadable"
