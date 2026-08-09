"""CLI-side validation for `task evidence --kind`.

The evidence kind accepted by the CLI must match the single source of truth
(`mac.models.EVIDENCE_KIND_CHOICES` / `normalize_evidence_kind`) that the runtime
service enforces, so an unsupported or mistyped kind fails fast at the CLI with a
consistent message instead of only after a round trip to the control plane.
"""

from __future__ import annotations

import io
import json
import sys

from mac.test_support import dsn_for
from mac.cli import main
from mac.models import EVIDENCE_KIND_CHOICES


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>`; return (rc, stdout_json_or_text, stderr)."""
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(["--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    raw = out.getvalue().strip()
    parsed = None
    if raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            parsed = raw
    return rc, parsed, err.getvalue()


def _claim(tmp_path, *, name="evidence-worker"):
    rc, machine, _ = _run(tmp_path, "admin", "machine", "register", name + "-host")
    assert rc == 0
    rc, agent, _ = _run(tmp_path, "agent", "register", machine["id"], name)
    assert rc == 0
    rc, task, _ = _run(tmp_path, "task", "create", "evidence kind validation")
    assert rc == 0
    rc, claimed, _ = _run(tmp_path, "task", "claim", task["id"], agent["id"])
    assert rc == 0
    return agent, task, claimed["lease_id"]


def test_evidence_kind_help_lists_canonical_choices(tmp_path):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        main(["--db", dsn_for(tmp_path), "task", "evidence", "--help"])
    except SystemExit as exc:
        assert exc.code == 0
    finally:
        sys.stdout = old
    text = out.getvalue()
    for choice in EVIDENCE_KIND_CHOICES:
        assert choice in text


def test_evidence_rejects_unsupported_kind_at_cli(tmp_path):
    agent, task, lease_id = _claim(tmp_path)

    rc, out, err = _run(
        tmp_path,
        "task", "evidence", task["id"],
        "--kind", "bogus",
        "--uri", "ci://build/1",
        "--summary", "should be rejected",
        "--created-by", agent["id"],
        "--lease-id", lease_id,
    )
    assert rc == 1
    assert out is None
    assert "unsupported evidence kind: bogus" in err
    for choice in EVIDENCE_KIND_CHOICES:
        assert choice in err


def test_evidence_normalizes_case_and_whitespace(tmp_path):
    agent, task, lease_id = _claim(tmp_path)

    rc, ev, _ = _run(
        tmp_path,
        "task", "evidence", task["id"],
        "--kind", "  TEST ",
        "--uri", "ci://build/2",
        "--summary", "case-insensitive kind",
        "--created-by", agent["id"],
        "--lease-id", lease_id,
    )
    assert rc == 0
    assert ev["kind"] == "test"
