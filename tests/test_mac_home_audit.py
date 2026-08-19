"""Tests for the read-only unified ``$MAC_HOME`` auditor (mac.mac_home_audit).

Coverage:
  * canonical target-layout recognition,
  * legacy pre-Phase-2 flat-root classification (with canonical target named),
  * drift on unknown top-level entries,
  * deeper non-standard-entry detection inside canonical containers,
  * missing-expected-path reporting,
  * missing / non-directory / unreadable root handling (never raises),
  * schema + summary shape,
  * a read-only assertion: snapshot the fixture tree (listing + mtimes) before
    and after the audit and assert nothing changed.
"""

from __future__ import annotations

import os
from pathlib import Path

from mac import mac_home_audit
from mac.mac_home_audit import (
    MAC_HOME_AUDIT_SCHEMA,
    MAC_HOME_SPEC,
    audit_mac_home,
)


# --- Fixture builders -------------------------------------------------------


def _touch(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x", encoding="utf-8")


def _make_canonical_root(base: Path) -> Path:
    root = base / "mac-canonical"
    (root / "ledger").mkdir(parents=True)
    _touch(root / "ledger" / "mac.db")
    (root / "ledger" / "backups").mkdir()
    (root / "ledger" / "archive").mkdir()
    (root / "secrets").mkdir()
    _touch(root / "secrets" / "mac.env")
    _touch(root / "secrets" / "client-principals.json")
    (root / "fleet").mkdir()
    _touch(root / "fleet" / "fleets.yaml")
    (root / "runtime").mkdir()
    (root / "runtime" / "journal").mkdir()
    (root / "gateway").mkdir()
    (root / "gateway" / "openclaw").mkdir()
    (root / "toolchain").mkdir()
    (root / "toolchain" / "venv").mkdir()
    return root


def _make_legacy_root(base: Path) -> Path:
    """Today's flat pre-Phase-2 root: datums directly under the root."""
    root = base / "mac-legacy"
    root.mkdir(parents=True)
    _touch(root / "mac.db")
    _touch(root / "mac.env")
    _touch(root / "fleets.yaml")
    _touch(root / "client-principals.json")
    (root / "openclaw").mkdir()
    (root / "journal").mkdir()
    (root / "backups").mkdir()
    return root


def _snapshot(root: Path) -> dict[str, tuple[int, float, int]]:
    """Map of relpath -> (lstat mode, mtime_ns, size) for the whole tree."""
    snap: dict[str, tuple[int, float, int]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in list(dirnames) + list(filenames):
            p = Path(dirpath) / name
            st = os.lstat(p)
            snap[str(p.relative_to(root))] = (st.st_mode, st.st_mtime_ns, st.st_size)
    return snap


# --- Schema / summary shape -------------------------------------------------


def test_schema_and_summary_shape(tmp_path):
    root = _make_canonical_root(tmp_path)
    report = audit_mac_home(root=root)

    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA == "mac.mac_home_audit.v1"
    assert report["root_path"] == str(root)
    assert report["root_exists"] is True
    assert report["status"] == "ok"
    # ISO-8601 UTC timestamp.
    assert report["audited_at"].endswith("+00:00")

    for key in ("entries", "non_standard_deeper", "missing_expected",
                "duplicates", "orphans", "summary"):
        assert key in report
    # Reserved keys are present and empty (sibling task populates them).
    assert report["duplicates"] == []
    assert report["orphans"] == []

    summary = report["summary"]
    for key in ("canonical", "legacy_accepted", "drift",
                "non_standard_deeper", "missing_expected"):
        assert key in summary
    assert sum(summary[k] for k in ("canonical", "legacy_accepted", "drift")) == \
        len(report["entries"])


# --- Canonical recognition --------------------------------------------------


def test_canonical_layout_recognized(tmp_path):
    root = _make_canonical_root(tmp_path)
    report = audit_mac_home(root=root)

    by_name = {e["name"]: e for e in report["entries"]}
    for bucket in ("ledger", "secrets", "fleet", "runtime", "gateway", "toolchain"):
        assert by_name[bucket]["generation"] == "canonical"
        assert by_name[bucket]["canonical_target"] == bucket

    assert report["summary"]["canonical"] == 6
    assert report["summary"]["drift"] == 0
    assert report["missing_expected"] == []


# --- Legacy classification --------------------------------------------------


def test_legacy_accepted_classification(tmp_path):
    root = _make_legacy_root(tmp_path)
    report = audit_mac_home(root=root)

    by_name = {e["name"]: e for e in report["entries"]}
    assert by_name["mac.db"]["generation"] == "legacy_accepted"
    assert by_name["mac.db"]["canonical_target"] == "ledger/mac.db"
    assert by_name["mac.env"]["generation"] == "legacy_accepted"
    assert by_name["mac.env"]["canonical_target"] == "secrets/mac.env"
    assert by_name["fleets.yaml"]["canonical_target"] == "fleet/fleets.yaml"
    assert by_name["openclaw"]["canonical_target"] == "gateway/openclaw"
    assert by_name["journal"]["canonical_target"] == "runtime/journal"

    # Every entry accepted; nothing drifts, and required buckets are covered by
    # their legacy stand-ins so no missing-expected is reported.
    assert report["summary"]["drift"] == 0
    assert report["summary"]["legacy_accepted"] == len(report["entries"])
    assert report["missing_expected"] == []


# --- Drift ------------------------------------------------------------------


def test_drift_on_unknown_top_level(tmp_path):
    root = _make_legacy_root(tmp_path)
    (root / "totally-unknown").mkdir()
    _touch(root / "stray.txt")
    report = audit_mac_home(root=root)

    by_name = {e["name"]: e for e in report["entries"]}
    assert by_name["totally-unknown"]["generation"] == "drift"
    assert by_name["totally-unknown"]["canonical_target"] is None
    assert by_name["stray.txt"]["generation"] == "drift"
    assert report["summary"]["drift"] == 2


def test_non_standard_deeper_entries(tmp_path):
    root = _make_canonical_root(tmp_path)
    # A stray file inside the canonical ledger/ container.
    _touch(root / "ledger" / "surprise.log")
    report = audit_mac_home(root=root)

    paths = {d["path"] for d in report["non_standard_deeper"]}
    assert "ledger/surprise.log" in paths
    assert report["summary"]["non_standard_deeper"] >= 1


# --- Missing expected paths -------------------------------------------------


def test_missing_expected_child(tmp_path):
    root = _make_canonical_root(tmp_path)
    # Remove a required child of a canonical bucket.
    (root / "ledger" / "mac.db").unlink()
    report = audit_mac_home(root=root)

    missing = {m["path"] for m in report["missing_expected"]}
    assert "ledger/mac.db" in missing
    assert report["summary"]["missing_expected"] >= 1


def test_missing_required_bucket_without_legacy(tmp_path):
    root = tmp_path / "mostly-empty"
    root.mkdir()
    # Only an unrelated dir; no ledger/secrets/fleet/runtime nor legacy stand-ins.
    (root / "unrelated").mkdir()
    report = audit_mac_home(root=root)

    missing = {m["path"] for m in report["missing_expected"]}
    for bucket in ("ledger", "secrets", "fleet", "runtime"):
        assert bucket in missing


# --- Missing / bad root handling (never raises) -----------------------------


def test_missing_root(tmp_path):
    report = audit_mac_home(root=tmp_path / "does-not-exist")
    assert report["root_exists"] is False
    assert report["status"] == "root_missing"
    assert report["entries"] == []
    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA


def test_root_is_file(tmp_path):
    f = tmp_path / "not-a-dir"
    f.write_text("hello", encoding="utf-8")
    report = audit_mac_home(root=f)
    assert report["root_exists"] is False
    assert report["status"] == "root_not_a_directory"


# --- Resolver plumbing ------------------------------------------------------


def test_defaults_resolve_via_mac_paths(tmp_path, monkeypatch):
    """With no explicit root, the audit resolves through mac.mac_paths."""
    monkeypatch.setenv("MAC_HOME", str(_make_legacy_root(tmp_path)))
    # Ensure derived homes don't point at real host dirs.
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("MAC_OPENCLAW_HOST_DIR", raising=False)
    report = audit_mac_home()
    assert report["root_exists"] is True
    assert report["root_path"] == str(tmp_path / "mac-legacy")


# --- Read-only guarantee ----------------------------------------------------


def test_audit_is_read_only(tmp_path):
    root = _make_canonical_root(tmp_path)
    _touch(root / "ledger" / "surprise.log")  # exercise deeper walk too
    before = _snapshot(root)

    audit_mac_home(root=root)

    after = _snapshot(root)
    assert before == after, "audit_mac_home must not mutate the tree"


# --- Spec is declarative data -----------------------------------------------


def test_spec_is_declarative():
    names = MAC_HOME_SPEC.bucket_names()
    assert names == {"ledger", "secrets", "fleet", "runtime", "gateway", "toolchain"}
    ledger = MAC_HOME_SPEC.bucket("ledger")
    assert ledger is not None
    child_names = {c.name for c in ledger.children}
    assert {"mac.db", "backups", "archive"} <= child_names


def test_no_hermes_private_import():
    """The auditor must not import from the vendored ``src/mac/_hermes`` snapshot."""
    src = Path(mac_home_audit.__file__).read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "_hermes" not in stripped, stripped
