"""Tests for mac.hermes_home_audit."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from mac.hermes_home_audit import (
    HERMES_HOME_AUDIT_SCHEMA,
    audit_hermes_home,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_home(tmp_path: Path) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    return home


def write_jobs(home: Path, jobs: list) -> Path:
    cron = home / "cron"
    cron.mkdir(exist_ok=True)
    jobs_file = cron / "jobs.json"
    jobs_file.write_text(json.dumps(jobs), encoding="utf-8")
    return jobs_file


# ---------------------------------------------------------------------------
# Shared audit fixtures
#
# Several tests audit the *same* home layout and only differ in which field of
# the resulting report they assert on. Computing the audit once per module (via
# tmp_path_factory, since tmp_path is function-scoped) avoids re-running the
# identical scan for every such test while keeping each named test and its
# assertion intact.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def empty_home_report(tmp_path_factory) -> dict:
    """Audit report for a bare, freshly-created hermes home (no scripts/cron)."""
    home = tmp_path_factory.mktemp("hermes_empty") / ".hermes"
    home.mkdir()
    return audit_hermes_home(home)


@pytest.fixture(scope="module")
def empty_cron_dir_report(tmp_path_factory) -> dict:
    """Audit report for a home whose cron dir exists but has no jobs file."""
    home = tmp_path_factory.mktemp("hermes_cron") / ".hermes"
    home.mkdir()
    (home / "cron").mkdir()
    return audit_hermes_home(home)


# ---------------------------------------------------------------------------
# Schema / structure
# ---------------------------------------------------------------------------


def test_audit_returns_correct_schema_key(empty_home_report):
    assert empty_home_report["schema"] == HERMES_HOME_AUDIT_SCHEMA


def test_audit_includes_required_top_level_keys(empty_home_report):
    for key in ("schema", "home_path", "audited_at", "home_exists",
                "scripts", "cron_jobs", "cron_error", "nonstandard_paths", "summary"):
        assert key in empty_home_report, f"missing key: {key}"


def test_audit_home_path_is_absolute(empty_home_report):
    assert Path(empty_home_report["home_path"]).is_absolute()


# ---------------------------------------------------------------------------
# Missing / nonexistent home
# ---------------------------------------------------------------------------


def test_audit_missing_home_sets_home_exists_false(tmp_path):
    report = audit_hermes_home(tmp_path / "no_such_dir")
    assert report["home_exists"] is False


def test_audit_missing_home_has_empty_lists(tmp_path):
    report = audit_hermes_home(tmp_path / "no_such_dir")
    assert report["scripts"] == []
    assert report["cron_jobs"] == []
    assert report["nonstandard_paths"] == []
    assert report["cron_error"] is None


def test_audit_missing_home_summary_zeros(tmp_path):
    report = audit_hermes_home(tmp_path / "no_such_dir")
    s = report["summary"]
    assert s["script_count"] == 0
    assert s["cron_job_count"] == 0
    assert s["enabled_cron_count"] == 0
    assert s["nonstandard_count"] == 0


# ---------------------------------------------------------------------------
# Scripts inventory
# ---------------------------------------------------------------------------


def test_audit_detects_sh_script_in_bin(tmp_path):
    home = make_home(tmp_path)
    bindir = home / "bin"
    bindir.mkdir()
    script = bindir / "backup.sh"
    script.write_text("#!/bin/bash\necho hi", encoding="utf-8")
    script.chmod(0o755)

    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["scripts"]]
    assert "bin/backup.sh" in paths


def test_audit_detects_py_script_in_scripts_dir(tmp_path):
    home = make_home(tmp_path)
    sdir = home / "scripts"
    sdir.mkdir()
    script = sdir / "my_tool.py"
    script.write_text("print('hello')", encoding="utf-8")

    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["scripts"]]
    assert "scripts/my_tool.py" in paths


def test_audit_detects_executable_in_hooks(tmp_path):
    home = make_home(tmp_path)
    hdir = home / "hooks"
    hdir.mkdir()
    hook = hdir / "pre-session"
    hook.write_text("#!/bin/sh\nexit 0", encoding="utf-8")
    hook.chmod(0o755)

    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["scripts"]]
    assert "hooks/pre-session" in paths


def test_audit_script_entry_has_expected_fields(tmp_path):
    home = make_home(tmp_path)
    bindir = home / "bin"
    bindir.mkdir()
    script = bindir / "run.sh"
    script.write_text("#!/bin/bash", encoding="utf-8")
    script.chmod(0o755)

    report = audit_hermes_home(home)
    entry = next(e for e in report["scripts"] if e["path"] == "bin/run.sh")
    assert entry["executable"] is True
    assert entry["extension"] == ".sh"
    assert "source" in entry


def test_audit_nonexecutable_py_still_included(tmp_path):
    home = make_home(tmp_path)
    sdir = home / "scripts"
    sdir.mkdir()
    script = sdir / "helper.py"
    script.write_text("x = 1", encoding="utf-8")
    script.chmod(0o644)

    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["scripts"]]
    assert "scripts/helper.py" in paths


def test_audit_script_count_in_summary(tmp_path):
    home = make_home(tmp_path)
    bindir = home / "bin"
    bindir.mkdir()
    for i in range(3):
        s = bindir / ("script%d.sh" % i)
        s.write_text("#!/bin/sh", encoding="utf-8")
        s.chmod(0o755)

    report = audit_hermes_home(home)
    assert report["summary"]["script_count"] == 3


def test_audit_no_scripts_returns_empty_list(empty_home_report):
    assert empty_home_report["scripts"] == []


def test_audit_detects_toplevel_executable_file(tmp_path):
    home = make_home(tmp_path)
    script = home / "setup.sh"
    script.write_text("#!/bin/sh\necho setup", encoding="utf-8")
    script.chmod(0o755)

    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["scripts"]]
    assert "setup.sh" in paths


# ---------------------------------------------------------------------------
# Cron jobs inventory
# ---------------------------------------------------------------------------


def test_audit_no_cron_dir_returns_empty_jobs(empty_home_report):
    assert empty_home_report["cron_jobs"] == []
    assert empty_home_report["cron_error"] is None


def test_audit_reads_cron_jobs_basic(tmp_path):
    home = make_home(tmp_path)
    jobs = [
        {"id": "abc123", "name": "daily-backup", "schedule": "0 2 * * *",
         "command": "backup.sh", "enabled": True},
    ]
    write_jobs(home, jobs)

    report = audit_hermes_home(home)
    assert len(report["cron_jobs"]) == 1
    j = report["cron_jobs"][0]
    assert j["id"] == "abc123"
    assert j["name"] == "daily-backup"
    assert j["schedule"] == "0 2 * * *"
    assert j["command"] == "backup.sh"
    assert j["enabled"] is True


def test_audit_cron_job_enabled_false(tmp_path):
    home = make_home(tmp_path)
    jobs = [{"id": "x1", "schedule": "* * * * *", "command": "echo hi", "enabled": False}]
    write_jobs(home, jobs)

    report = audit_hermes_home(home)
    assert report["cron_jobs"][0]["enabled"] is False


def test_audit_cron_enabled_defaults_to_true(tmp_path):
    home = make_home(tmp_path)
    jobs = [{"id": "x1", "schedule": "* * * * *", "command": "echo hi"}]
    write_jobs(home, jobs)

    report = audit_hermes_home(home)
    assert report["cron_jobs"][0]["enabled"] is True


@pytest.mark.parametrize(
    "schedule, expected_substrings",
    [
        pytest.param(
            {"type": "cron", "expression": "*/5 * * * *"},
            ["cron", "*/5"],
            id="normalised",
        ),
        pytest.param(
            {"type": "cron", "expression": "0 6 * * 1"},
            ["cron"],
            id="expression-key",
        ),
        pytest.param(
            {"type": "interval", "cron": "*/5 * * * *"},
            ["interval"],
            id="cron-key",
        ),
    ],
)
def test_audit_cron_dict_schedule_variants(tmp_path, schedule, expected_substrings):
    home = make_home(tmp_path)
    write_jobs(home, [
        {"id": "j1", "schedule": schedule, "command": "echo x", "enabled": True},
    ])

    report = audit_hermes_home(home)
    sched = report["cron_jobs"][0]["schedule"]
    assert sched is not None
    for substring in expected_substrings:
        assert substring in sched


def test_audit_cron_prompt_field_used_as_command(tmp_path):
    home = make_home(tmp_path)
    jobs = [{"id": "j2", "schedule": "0 * * * *", "prompt": "run daily task", "enabled": True}]
    write_jobs(home, jobs)

    report = audit_hermes_home(home)
    assert report["cron_jobs"][0]["command"] == "run daily task"


def test_audit_cron_summary_counts(tmp_path):
    home = make_home(tmp_path)
    jobs = [
        {"id": "a", "schedule": "* * * * *", "command": "cmd1", "enabled": True},
        {"id": "b", "schedule": "* * * * *", "command": "cmd2", "enabled": False},
        {"id": "c", "schedule": "* * * * *", "command": "cmd3", "enabled": True},
    ]
    write_jobs(home, jobs)

    report = audit_hermes_home(home)
    assert report["summary"]["cron_job_count"] == 3
    assert report["summary"]["enabled_cron_count"] == 2


def test_audit_cron_invalid_json_reports_error(tmp_path):
    home = make_home(tmp_path)
    cron = home / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text("NOT VALID JSON{{{", encoding="utf-8")

    report = audit_hermes_home(home)
    assert report["cron_jobs"] == []
    assert report["cron_error"] is not None
    assert "json_error" in report["cron_error"]


def test_audit_cron_non_list_json_reports_error(tmp_path):
    home = make_home(tmp_path)
    cron = home / "cron"
    cron.mkdir()
    (cron / "jobs.json").write_text('{"key": "val"}', encoding="utf-8")

    report = audit_hermes_home(home)
    assert report["cron_error"] is not None
    assert "unexpected_format" in report["cron_error"]


def test_audit_cron_empty_jobs_list(tmp_path):
    home = make_home(tmp_path)
    write_jobs(home, [])
    report = audit_hermes_home(home)
    assert report["cron_jobs"] == []
    assert report["cron_error"] is None


# ---------------------------------------------------------------------------
# Non-standard paths
# ---------------------------------------------------------------------------


def test_audit_known_entry_not_flagged_nonstandard(tmp_path):
    home = make_home(tmp_path)
    (home / "config.yaml").write_text("key: val", encoding="utf-8")

    report = audit_hermes_home(home)
    known_paths = [e["path"] for e in report["nonstandard_paths"]]
    assert "config.yaml" not in known_paths


def test_audit_unknown_file_flagged_nonstandard(tmp_path):
    home = make_home(tmp_path)
    (home / "mystery.txt").write_text("data", encoding="utf-8")

    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["nonstandard_paths"]]
    assert "mystery.txt" in paths


def test_audit_unknown_directory_flagged_nonstandard(tmp_path):
    home = make_home(tmp_path)
    (home / "custom_project").mkdir()

    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["nonstandard_paths"]]
    assert "custom_project" in paths


def test_audit_nonstandard_entry_kind_file(tmp_path):
    home = make_home(tmp_path)
    f = home / "extra.dat"
    f.write_text("x", encoding="utf-8")

    report = audit_hermes_home(home)
    entry = next(e for e in report["nonstandard_paths"] if e["path"] == "extra.dat")
    assert entry["kind"] == "file"
    assert entry["size_bytes"] == 1


def test_audit_nonstandard_entry_kind_directory(tmp_path):
    home = make_home(tmp_path)
    (home / "odd_dir").mkdir()

    report = audit_hermes_home(home)
    entry = next(e for e in report["nonstandard_paths"] if e["path"] == "odd_dir")
    assert entry["kind"] == "directory"
    assert entry["size_bytes"] is None


def test_audit_nonstandard_count_in_summary(tmp_path):
    home = make_home(tmp_path)
    (home / "alpha").mkdir()
    (home / "beta").mkdir()
    (home / "gamma.txt").write_text("g")

    report = audit_hermes_home(home)
    assert report["summary"]["nonstandard_count"] == 3


def test_audit_cron_dir_not_flagged_nonstandard(empty_cron_dir_report):
    paths = [e["path"] for e in empty_cron_dir_report["nonstandard_paths"]]
    assert "cron" not in paths


def test_audit_nonstandard_symlink_identified(tmp_path):
    home = make_home(tmp_path)
    target = tmp_path / "somewhere"
    target.mkdir()
    link = home / "my_link"
    link.symlink_to(target)

    report = audit_hermes_home(home)
    entries = {e["path"]: e for e in report["nonstandard_paths"]}
    if "my_link" in entries:
        assert entries["my_link"]["kind"] == "symlink"


# ---------------------------------------------------------------------------
# Full integration scenario
# ---------------------------------------------------------------------------


def test_audit_full_scenario(tmp_path):
    home = make_home(tmp_path)

    # scripts
    (home / "bin").mkdir()
    script = home / "bin" / "deploy.sh"
    script.write_text("#!/bin/bash\necho deploy")
    script.chmod(0o755)

    # cron
    jobs = [
        {"id": "j1", "name": "nightly", "schedule": "0 0 * * *",
         "command": "nightly.sh", "enabled": True},
        {"id": "j2", "name": "disabled", "schedule": "0 0 * * *",
         "command": "off.sh", "enabled": False},
    ]
    write_jobs(home, jobs)

    # known entries (should not appear in nonstandard)
    (home / "config.yaml").write_text("k: v")
    (home / "skills").mkdir()

    # non-standard entries
    (home / "my_project").mkdir()
    (home / "notes.md").write_text("hello")

    report = audit_hermes_home(home)

    assert report["schema"] == HERMES_HOME_AUDIT_SCHEMA
    assert report["home_exists"] is True
    assert report["summary"]["script_count"] >= 1
    assert report["summary"]["cron_job_count"] == 2
    assert report["summary"]["enabled_cron_count"] == 1
    assert report["summary"]["nonstandard_count"] == 2

    ns_paths = {e["path"] for e in report["nonstandard_paths"]}
    assert "my_project" in ns_paths
    assert "notes.md" in ns_paths
    assert "config.yaml" not in ns_paths
    assert "skills" not in ns_paths


# ---------------------------------------------------------------------------
# Additional coverage: empty cron dir, source field values, dict schedule variants
# ---------------------------------------------------------------------------


def test_audit_cron_dir_exists_but_no_jobs_file(empty_cron_dir_report):
    assert empty_cron_dir_report["cron_jobs"] == []
    assert empty_cron_dir_report["cron_error"] is None


def test_audit_script_source_field_reflects_scan_dir(tmp_path):
    home = make_home(tmp_path)
    (home / "scripts").mkdir()
    script = home / "scripts" / "util.py"
    script.write_text("# util", encoding="utf-8")

    report = audit_hermes_home(home)
    entry = next(e for e in report["scripts"] if e["path"] == "scripts/util.py")
    assert entry["source"] == "scripts"


def test_audit_script_source_hooks(tmp_path):
    home = make_home(tmp_path)
    (home / "hooks").mkdir()
    hook = home / "hooks" / "post-run.sh"
    hook.write_text("#!/bin/sh\nexit 0", encoding="utf-8")
    hook.chmod(0o755)

    report = audit_hermes_home(home)
    entry = next(e for e in report["scripts"] if "hooks" in e["path"])
    assert entry["source"] == "hooks"


def test_audit_cron_task_field_used_as_command(tmp_path):
    home = make_home(tmp_path)
    write_jobs(home, [
        {"id": "t1", "schedule": "* * * * *", "task": "do something", "enabled": True},
    ])

    report = audit_hermes_home(home)
    j = report["cron_jobs"][0]
    assert j["command"] == "do something"


def test_audit_script_extension_none_for_executable_no_ext(tmp_path):
    home = make_home(tmp_path)
    (home / "bin").mkdir()
    exe = home / "bin" / "runner"
    exe.write_text("#!/bin/sh\n", encoding="utf-8")
    exe.chmod(0o755)

    report = audit_hermes_home(home)
    entry = next(e for e in report["scripts"] if e["path"] == "bin/runner")
    assert entry["extension"] is None
    assert entry["executable"] is True


# ---------------------------------------------------------------------------
# Allow-list reuse helpers + extra branch coverage (public API unchanged)
# ---------------------------------------------------------------------------


def test_known_top_level_is_public_and_reused():
    from mac.hermes_home_audit import HERMES_KNOWN_TOP_LEVEL, _KNOWN_TOP_LEVEL

    assert HERMES_KNOWN_TOP_LEVEL is _KNOWN_TOP_LEVEL
    assert "SOUL.md" in HERMES_KNOWN_TOP_LEVEL
    assert "config.yaml" in HERMES_KNOWN_TOP_LEVEL
    assert "cron" in HERMES_KNOWN_TOP_LEVEL


def test_classify_named_children_helper(tmp_path):
    from mac.hermes_home_audit import classify_named_children

    d = tmp_path / "c"
    d.mkdir()
    (d / "keep").write_text("1", encoding="utf-8")
    (d / "drop").write_text("2", encoding="utf-8")
    items = classify_named_children(d, {"keep"}, container="box")
    by_name = {item["name"]: item for item in items}
    assert by_name["keep"]["classification"] == "canonical"
    assert by_name["keep"]["path"] == "box/keep"
    assert by_name["drop"]["classification"] == "drift"


def test_classify_named_children_oserror_kind(tmp_path, monkeypatch):
    from mac.hermes_home_audit import classify_named_children

    d = tmp_path / "c"
    d.mkdir()
    ghost = d / "x"
    ghost.write_text("1", encoding="utf-8")
    real_is_symlink = Path.is_symlink

    def boom(self):
        if self == ghost:
            raise OSError("gone")
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", boom)
    items = classify_named_children(d, set())
    assert items[0]["kind"] == "other"


def test_default_home_uses_mac_paths(tmp_path, monkeypatch):
    gw = tmp_path / "gw"
    gw.mkdir()
    (gw / "config.yaml").write_text("x", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(gw))
    report = audit_hermes_home()
    assert report["home_path"] == str(gw.resolve())
    assert report["status"] == "ok"


def test_audit_not_a_directory_status(tmp_path):
    target = tmp_path / "file"
    target.write_text("x", encoding="utf-8")
    report = audit_hermes_home(target)
    assert report["status"] == "not_a_directory"
    assert report["home_exists"] is False


def test_audit_unreadable_directory(tmp_path):
    home = tmp_path / "locked"
    home.mkdir()
    home.chmod(0)
    try:
        report = audit_hermes_home(home)
    finally:
        home.chmod(0o700)
    assert report["status"] in {"unreadable", "ok"}
    if report["status"] == "unreadable":
        assert report["scripts"] == []


def test_cron_skips_non_dict_items_and_empty_ids(tmp_path):
    home = make_home(tmp_path)
    write_jobs(
        home,
        [
            "not-a-dict",
            {"id": "  ", "name": "", "schedule": None, "command": None},
            {"id": "ok", "schedule": {"type": "", "expression": ""}, "enabled": True},
        ],
    )
    report = audit_hermes_home(home)
    assert report["cron_error"] is None
    assert len(report["cron_jobs"]) == 2
    assert report["cron_jobs"][0]["id"] is None
    assert report["cron_jobs"][0]["schedule"] is None
    assert report["cron_jobs"][1]["schedule"] is None


def test_cron_read_error(tmp_path, monkeypatch):
    from mac import hermes_home_audit as hha

    home = make_home(tmp_path)
    write_jobs(home, [{"id": "x"}])
    real_read = Path.read_text

    def boom(self, *args, **kwargs):
        if self.name == "jobs.json":
            raise OSError("nope")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", boom)
    jobs, err = hha._load_cron_jobs(home)
    assert jobs == []
    assert err is not None and err.startswith("read_error:")


def test_executable_stat_oserror_and_rel_fallback(tmp_path, monkeypatch):
    from mac import hermes_home_audit as hha

    home = make_home(tmp_path)
    ghost = tmp_path / "ghost"
    real_stat = Path.stat

    def boom(self, *args, **kwargs):
        if self == ghost:
            raise OSError("stat")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", boom)
    assert hha._is_executable(ghost) is False
    assert hha._rel(home, tmp_path / "elsewhere") == str(tmp_path / "elsewhere")


def test_safe_iterdir_oserror(tmp_path, monkeypatch):
    from mac.hermes_home_audit import safe_iterdir

    def boom(self):
        raise OSError("list fail")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert safe_iterdir(tmp_path) == []


def test_dedup_top_level_and_scripts_overlap(tmp_path):
    """A root-level file is not re-listed; overlapping dir scans still unique."""
    home = make_home(tmp_path)
    (home / "scripts").mkdir()
    script = home / "scripts" / "util.py"
    script.write_text("x\n", encoding="utf-8")
    # Also drop a top-level script so both collectors run in one audit.
    top = home / "setup.sh"
    top.write_text("#!/bin/sh\n", encoding="utf-8")
    top.chmod(0o755)
    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["scripts"]]
    assert paths == ["scripts/util.py", "setup.sh"] or set(paths) == {
        "scripts/util.py",
        "setup.sh",
    }


def test_non_script_in_scripts_dir_skipped(tmp_path):
    home = make_home(tmp_path)
    (home / "scripts").mkdir()
    (home / "scripts" / "notes.txt").write_text("nope\n", encoding="utf-8")
    (home / "scripts" / "ok.py").write_text("x\n", encoding="utf-8")
    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["scripts"]]
    assert paths == ["scripts/ok.py"]


def test_nonstandard_fifo_kind_other(tmp_path):
    home = make_home(tmp_path)
    os.mkfifo(home / "a-fifo")
    report = audit_hermes_home(home)
    entry = next(e for e in report["nonstandard_paths"] if e["path"] == "a-fifo")
    assert entry["kind"] == "other"


def test_scripts_in_plugins_and_pruned_install_dirs(tmp_path):
    home = make_home(tmp_path)
    plugins = home / "plugins"
    plugins.mkdir()
    (plugins / "tool.py").write_text("print(1)\n", encoding="utf-8")
    nested = home / "skills" / "node_modules"
    nested.mkdir(parents=True)
    (nested / "skip.py").write_text("x\n", encoding="utf-8")
    (home / "skills" / "ok.py").write_text("x\n", encoding="utf-8")
    report = audit_hermes_home(home)
    paths = {e["path"] for e in report["scripts"]}
    assert "plugins/tool.py" in paths
    assert "skills/ok.py" in paths
    assert not any("node_modules" in p for p in paths)


def test_fifo_classified_as_other_kind(tmp_path):
    from mac.hermes_home_audit import classify_named_children

    home = make_home(tmp_path)
    fifo = home / "a-fifo"
    os.mkfifo(fifo)
    items = classify_named_children(home, set())
    kinds = {item["name"]: item["kind"] for item in items}
    assert kinds["a-fifo"] == "other"


def test_nonstandard_file_size_oserror(tmp_path, monkeypatch):
    from mac import hermes_home_audit as hha

    home = make_home(tmp_path)
    mystery = home / "mystery.bin"
    mystery.write_text("abc", encoding="utf-8")
    real_stat = Path.stat

    def boom(self, *args, follow_symlinks=True, **kwargs):
        result = real_stat(self, *args, follow_symlinks=follow_symlinks, **kwargs)
        if self == mystery and follow_symlinks:

            class _Stat:
                def __init__(self, inner):
                    self._inner = inner

                def __getattr__(self, name):
                    if name == "st_size":
                        raise OSError("size")
                    return getattr(self._inner, name)

            return _Stat(result)
        return result

    monkeypatch.setattr(Path, "stat", boom)
    results = hha._collect_nonstandard(home)
    entry = next(e for e in results if e["path"] == "mystery.bin")
    assert entry["size_bytes"] is None


def test_exists_oserror_status(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    real_exists = Path.exists

    def boom(self):
        if self == home.resolve() or self == home:
            raise OSError("x")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", boom)
    report = audit_hermes_home(home)
    assert report["status"] == "unreadable"


def test_is_dir_oserror_status(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    real_is_dir = Path.is_dir

    def boom(self):
        if self == home.resolve() or self == home:
            raise OSError("x")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", boom)
    report = audit_hermes_home(home)
    assert report["status"] == "unreadable"


def test_collect_helpers_on_nondir(tmp_path):
    from mac import hermes_home_audit as hha

    blob = tmp_path / "not-a-dir"
    blob.write_text("x", encoding="utf-8")
    assert hha._collect_scripts(blob) == []
    assert hha._collect_nonstandard(blob) == []
    assert hha._is_script(blob.parent) is False


def test_cron_numeric_schedule(tmp_path):
    home = make_home(tmp_path)
    write_jobs(home, [{"id": "n", "schedule": 5, "command": "x", "enabled": True}])
    report = audit_hermes_home(home)
    assert report["cron_jobs"][0]["schedule"] == "5"


def test_listdir_oserror_marks_unreadable(tmp_path, monkeypatch):
    home = make_home(tmp_path)
    real_listdir = os.listdir

    def boom(path):
        if Path(path) == home.resolve() or Path(path) == home:
            raise OSError("denied")
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", boom)
    report = audit_hermes_home(home)
    assert report["status"] == "unreadable"
