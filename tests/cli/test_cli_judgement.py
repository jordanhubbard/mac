"""CLI coverage for ``mac admin judgement status`` and ``mac admin judgement run``.

Local ``--db`` planes do not attach the hourly judgement process (that is a
hub concern). These tests exercise the parsers and confirm the local plane
refuses instead of inventing a process that would call live ``gh pr close``.
"""

from __future__ import annotations

import io
import json
import sys

from mac.test_support import dsn_for
from mac.cli import main


def _run_raw(tmp_path, *args):
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None, err.getvalue()


def _run(tmp_path, *args):
    rc, payload, _err = _run_raw(tmp_path, *args)
    return rc, payload


def test_judgement_status_local_plane_requires_attached_process(tmp_path):
    rc, payload = _run(tmp_path, "admin", "judgement", "status")
    assert rc == 1
    assert payload is None
    _rc, _payload, err = _run_raw(tmp_path, "admin", "judgement", "status")
    assert "not attached" in err


def test_judgement_run_local_plane_requires_attached_process(tmp_path):
    rc, payload = _run(tmp_path, "admin", "judgement", "run")
    assert rc == 1
    assert payload is None
    _rc, _payload, err = _run_raw(tmp_path, "admin", "judgement", "run")
    assert "not attached" in err
