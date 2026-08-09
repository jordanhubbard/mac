"""Behavioral tests for `mac fleet backlog-groom enable/disable`."""
from __future__ import annotations

import io
import json
import sys

from mac.test_support import control_plane_on, dsn_for, store_on
from mac.cli import main
from mac.services import ControlPlane
from mac.test_support import ephemeral_store


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _make_project(tmp_path, name="mac", url="https://github.com/o/r"):
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project(name, metadata={"repository_url": url})


def test_enable_sets_policy(tmp_path):
    _make_project(tmp_path)
    rc, out = _run(tmp_path, "admin", "fleet", "backlog-groom", "enable", "mac",
                   "--backlog-size", "7", "--capability", "python")
    assert rc in (None, 0)
    assert out["backlog_grooming"]["enabled"] is True
    assert out["backlog_grooming"]["backlog_size"] == 7
    assert out["backlog_grooming"]["default_capabilities"] == ["python"]

    cp = control_plane_on(dsn_for(tmp_path))
    record = cp.get_project_record("mac")
    assert record.metadata["repository_url"] == "https://github.com/o/r"
    assert record.metadata["backlog_grooming"]["enabled"] is True


def test_disable_flips_flag(tmp_path):
    _make_project(tmp_path)
    _run(tmp_path, "admin", "fleet", "backlog-groom", "enable", "mac")
    rc, out = _run(tmp_path, "admin", "fleet", "backlog-groom", "disable", "mac")
    assert rc in (None, 0)
    assert out["backlog_grooming"]["enabled"] is False


def test_enable_requires_project_record(tmp_path):
    control_plane_on(dsn_for(tmp_path))
    rc, _ = _run(tmp_path, "admin", "fleet", "backlog-groom", "enable", "ghost")
    assert rc not in (None, 0)
