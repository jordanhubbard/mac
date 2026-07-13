"""Tests for mac.hermes_home_audit."""

from __future__ import annotations

import json
import stat
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
# Schema / structure
# ---------------------------------------------------------------------------


def test_audit_returns_correct_schema_key(tmp_path):
    report = audit_hermes_home(make_home(tmp_path))
    assert report["schema"] == HERMES_HOME_AUDIT_SCHEMA


def test_audit_includes_required_top_level_keys(tmp_path):
    report = audit_hermes_home(make_home(tmp_path))
    for key in ("schema", "home_path", "audited_at", "home_exists",
                "scripts", "cron_jobs", "cron_error", "nonstandard_paths", "summary"):
        assert key in report, f"missing key: {key}"


def test_audit_home_path_is_absolute(tmp_path):
    report = audit_hermes_home(make_home(tmp_path))
    assert Path(report["home_path"]).is_absolute()


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


def test_audit_no_scripts_returns_empty_list(tmp_path):
    home = make_home(tmp_path)
    report = audit_hermes_home(home)
    assert report["scripts"] == []


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


def test_audit_no_cron_dir_returns_empty_jobs(tmp_path):
    home = make_home(tmp_path)
    report = audit_hermes_home(home)
    assert report["cron_jobs"] == []
    assert report["cron_error"] is None


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


def test_audit_cron_dict_schedule_normalised(tmp_path):
    home = make_home(tmp_path)
    jobs = [
        {"id": "j1", "schedule": {"type": "cron", "expression": "*/5 * * * *"},
         "command": "echo x", "enabled": True},
    ]
    write_jobs(home, jobs)

    report = audit_hermes_home(home)
    sched = report["cron_jobs"][0]["schedule"]
    assert "cron" in sched
    assert "*/5" in sched


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


def test_audit_cron_dir_not_flagged_nonstandard(tmp_path):
    home = make_home(tmp_path)
    cron = home / "cron"
    cron.mkdir()

    report = audit_hermes_home(home)
    paths = [e["path"] for e in report["nonstandard_paths"]]
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
