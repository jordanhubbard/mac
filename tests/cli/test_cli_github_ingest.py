"""Behavioral tests for `mac fleet github-ingest enable/disable`."""
from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.services import ControlPlane
from mac.store import SQLiteStore


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", str(tmp_path / "mac.db"), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _make_project(tmp_path, name="mac", url="https://github.com/o/r"):
    cp = ControlPlane(SQLiteStore(str(tmp_path / "mac.db")))
    cp.create_project(name, metadata={"repository_url": url})
    return cp


def test_github_ingest_enable_sets_policy(tmp_path):
    _make_project(tmp_path)
    rc, out = _run(
        tmp_path,
        "fleet",
        "github-ingest",
        "enable",
        "mac",
        "--label",
        "agent-ready",
        "--capability",
        "python",
    )
    assert rc in (None, 0)
    assert out["github_issue_ingest"]["enabled"] is True
    assert out["github_issue_ingest"]["labels"] == ["agent-ready"]
    assert out["github_issue_ingest"]["default_capabilities"] == ["python"]

    # Persisted onto the project record's metadata (repository_url preserved).
    cp = ControlPlane(SQLiteStore(str(tmp_path / "mac.db")))
    record = cp.get_project_record("mac")
    assert record.metadata["repository_url"] == "https://github.com/o/r"
    assert record.metadata["github_issue_ingest"]["enabled"] is True


def test_github_ingest_disable_flips_flag(tmp_path):
    _make_project(tmp_path)
    _run(tmp_path, "fleet", "github-ingest", "enable", "mac")
    rc, out = _run(tmp_path, "fleet", "github-ingest", "disable", "mac")
    assert rc in (None, 0)
    assert out["github_issue_ingest"]["enabled"] is False


def test_github_ingest_enable_requires_project_record(tmp_path):
    # A bare db with no such project record -> clear error, non-zero exit.
    ControlPlane(SQLiteStore(str(tmp_path / "mac.db")))
    rc, _out = _run(tmp_path, "fleet", "github-ingest", "enable", "ghost")
    assert rc not in (None, 0)
