"""Behavioral tests for `mac fleet target set/get/show/list`.

Smoke-covers the target-of-record CLI surface: set -> get returns the pinned
per-role target for both artifact tracks (source commit + OpenClaw
version/revision).
"""
from __future__ import annotations

import io
import json
import sys

from mac.cli import main


def _run(tmp_path, *args):
    """Invoke the CLI against a throwaway manifest under *tmp_path*.

    The ``--manifest`` override keeps the checked-in
    ``deploy/openclaw/fleet-target.json`` untouched.
    """
    manifest = str(tmp_path / "fleet-target.json")
    # Inject the manifest override right after the (domain, sub, subsub) prefix
    # so positional args still line up for argparse.
    argv = ["--json", *args, "--manifest", manifest]
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(argv)
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _seed_both_tracks(tmp_path):
    _run(
        tmp_path,
        "fleet", "target", "set", "gateway", "0f55d49",
        "--openclaw-version", "2026.6.11", "--openclaw-revision", "19",
    )
    _run(tmp_path, "fleet", "target", "set", "worker", "abc1234")


def test_set_then_get_returns_pinned_target(tmp_path):
    rc, out = _run(
        tmp_path,
        "fleet", "target", "set", "gateway", "0f55d49",
        "--openclaw-version", "2026.6.11", "--openclaw-revision", "19",
    )
    assert rc in (None, 0)
    assert out["role"] == "gateway"
    assert out["target"]["source"] == "0f55d49"
    assert out["target"]["openclaw"] == {"version": "2026.6.11", "revision": "19"}

    rc, got = _run(tmp_path, "fleet", "target", "get", "gateway")
    assert rc in (None, 0)
    assert got["target"]["source"] == "0f55d49"
    assert got["target"]["openclaw"]["revision"] == "19"


def test_worker_track_is_source_only(tmp_path):
    rc, out = _run(tmp_path, "fleet", "target", "set", "worker", "abc1234")
    assert rc in (None, 0)
    assert out["target"]["source"] == "abc1234"
    assert "openclaw" not in out["target"]


def test_list_is_not_empty_once_populated(tmp_path):
    _seed_both_tracks(tmp_path)
    rc, listed = _run(tmp_path, "fleet", "target", "list")
    assert rc in (None, 0)
    roles = {row["role"] for row in listed}
    assert roles == {"gateway", "worker"}


def test_show_returns_full_manifest(tmp_path):
    _seed_both_tracks(tmp_path)
    rc, shown = _run(tmp_path, "fleet", "target", "show")
    assert rc in (None, 0)
    assert shown["schema"] == "mac.fleet_target.v1"
    assert shown["roles"]["gateway"]["openclaw"]["version"] == "2026.6.11"


def test_get_missing_role_fails(tmp_path):
    _seed_both_tracks(tmp_path)
    rc, _ = _run(tmp_path, "fleet", "target", "get", "ghost")
    assert rc not in (None, 0)


def test_set_rejects_partial_openclaw_track(tmp_path):
    rc, _ = _run(
        tmp_path,
        "fleet", "target", "set", "gateway", "0f55d49",
        "--openclaw-version", "2026.6.11",
    )
    assert rc not in (None, 0)


def test_set_rejects_symbolic_source(tmp_path):
    rc, _ = _run(tmp_path, "fleet", "target", "set", "worker", "HEAD")
    assert rc not in (None, 0)
