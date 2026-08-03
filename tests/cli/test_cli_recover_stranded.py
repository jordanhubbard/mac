"""`mac task recover-stranded` is dry-run unless --apply.

The command mutates live task state in bulk, so the default must report
rather than act.
"""
from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.test_support import dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def test_recover_stranded_defaults_to_dry_run(tmp_path):
    rc, report = _run(tmp_path, "task", "recover-stranded")
    assert rc == 0
    assert report["schema"] == "mac.strand_recovery.v1"
    assert report["dry_run"] is True
    assert report["supervised"] == 0


def test_recover_stranded_apply_is_explicit(tmp_path):
    rc, report = _run(tmp_path, "task", "recover-stranded", "--apply")
    assert rc == 0
    assert report["dry_run"] is False
