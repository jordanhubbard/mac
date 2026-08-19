"""Read-only auditor of the unified $MAC_HOME layout (mac.mac_home_audit.v1)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from mac import mac_home_audit as mha
from mac.mac_home_audit import (
    CANONICAL_BUCKETS,
    CLASS_CANONICAL,
    CLASS_DRIFT,
    CLASS_LEGACY,
    GEN_MIXED,
    GEN_PRE_PHASE2,
    GEN_UNIFIED,
    GEN_UNKNOWN,
    LEGACY_ROOT_TARGETS,
    MAC_HOME_AUDIT_SCHEMA,
    UNIFIED_LAYOUT,
    audit_mac_home,
    expected_paths,
)


def _touch(path: Path, *, directory: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if directory:
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.write_text("x", encoding="utf-8")
    return path


def _snapshot(root: Path):
    rows = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        try:
            st = base.lstat()
            rel = "." if base == root else str(base.relative_to(root))
            rows.append((rel, st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid))
        except OSError:
            pass
        for name in sorted(dirnames + filenames):
            path = base / name
            try:
                st = path.lstat()
            except OSError:
                continue
            rel = str(path.relative_to(root))
            rows.append((rel, st.st_mtime_ns, st.st_size, st.st_mode, st.st_uid))
    return sorted(rows)


def _by_path(report):
    return {item["path"]: item for item in report["entries"]}


def test_schema_and_summary_shape(tmp_path):
    root = tmp_path / "mac-home"
    root.mkdir()
    report = audit_mac_home(root)
    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA
    assert report["schema"] == "mac.mac_home_audit.v1"
    assert report["root_path"] == str(root)
    assert report["root_exists"] is True
    assert report["status"] == "ok"
    assert report["audited_at"].endswith("Z")
    assert "T" in report["audited_at"]
    assert isinstance(report["entries"], list)
    assert isinstance(report["missing_expected"], list)
    assert report["duplicates"] == []
    assert report["orphans"] == []
    summary = report["summary"]
    for key in (
        "entry_count",
        "canonical",
        "legacy_accepted",
        "drift",
        "missing_expected",
        "duplicates",
        "orphans",
    ):
        assert key in summary
    assert summary["duplicates"] == 0
    assert summary["orphans"] == 0
    assert summary["entry_count"] == len(report["entries"])
    assert report["layout"]["buckets"] == [bucket.name for bucket in UNIFIED_LAYOUT]


def test_unified_layout_is_declarative_spec():
    names = [bucket.name for bucket in UNIFIED_LAYOUT]
    assert names == ["ledger", "secrets", "fleet", "runtime", "gateway", "toolchain"]
    children = {bucket.name: bucket.children for bucket in UNIFIED_LAYOUT}
    assert children["ledger"] == ("mac.db", "backups", "archive")
    assert children["secrets"] == ("mac.env", ".env", "client-principals.json")
    assert children["fleet"] == ("fleets.yaml", "specs")
    assert "journal" in children["runtime"]
    assert "openclaw" in children["gateway"]
    assert children["toolchain"] == ("src", "venv", "bin", "hermes-agent")
    rels = [item.relative for item in expected_paths()]
    assert "ledger/mac.db" in rels
    assert "gateway/openclaw" in rels
    assert "toolchain/hermes-agent" in rels


def test_canonical_layout_recognition(tmp_path):
    root = tmp_path / "unified"
    root.mkdir()
    for expected in expected_paths():
        path = root / expected.relative
        _touch(path, directory=expected.kind == "directory")
    report = audit_mac_home(root)
    by_path = _by_path(report)
    for bucket in UNIFIED_LAYOUT:
        assert by_path[bucket.name]["classification"] == CLASS_CANONICAL
        assert by_path[bucket.name]["generation"] == GEN_UNIFIED
        for child in bucket.children:
            rel = "%s/%s" % (bucket.name, child)
            assert by_path[rel]["classification"] == CLASS_CANONICAL
    assert report["layout"]["generation_detected"] == GEN_UNIFIED
    assert report["summary"]["legacy_accepted"] == 0
    assert report["summary"]["drift"] == 0
    assert report["missing_expected"] == []


def test_legacy_accepted_classification(tmp_path):
    root = tmp_path / "legacy"
    root.mkdir()
    _touch(root / "mac.db")
    _touch(root / "mac.env")
    _touch(root / "fleets.yaml")
    _touch(root / "openclaw", directory=True)
    _touch(root / "journal", directory=True)
    _touch(root / "backups", directory=True)
    report = audit_mac_home(root)
    by_path = _by_path(report)
    assert by_path["mac.db"]["classification"] == CLASS_LEGACY
    assert by_path["mac.db"]["canonical_target"] == "ledger/mac.db"
    assert by_path["mac.db"]["generation"] == GEN_PRE_PHASE2
    assert by_path["openclaw"]["canonical_target"] == "gateway/openclaw"
    assert report["layout"]["generation_detected"] == GEN_PRE_PHASE2
    satisfied = {item["path"]: item["satisfied_by_legacy"] for item in report["missing_expected"]}
    assert satisfied["ledger/mac.db"] == "mac.db"
    assert satisfied["secrets/mac.env"] == "mac.env"
    assert satisfied["fleet/fleets.yaml"] == "fleets.yaml"
    assert satisfied["gateway/openclaw"] == "openclaw"
    assert satisfied["runtime/journal"] == "journal"
    assert satisfied["ledger/backups"] == "backups"


def test_legacy_root_targets_cover_task_pre_phase2_shape():
    for name in ("mac.db", "mac.env", "fleets.yaml", "openclaw", "journal", "backups"):
        assert name in LEGACY_ROOT_TARGETS


def test_drift_on_unknown_top_level_entries(tmp_path):
    root = tmp_path / "drift"
    root.mkdir()
    _touch(root / "totally-unknown.bin")
    _touch(root / "random-dir", directory=True)
    report = audit_mac_home(root)
    by_path = _by_path(report)
    assert by_path["totally-unknown.bin"]["classification"] == CLASS_DRIFT
    assert by_path["totally-unknown.bin"]["canonical_target"] is None
    assert by_path["random-dir"]["classification"] == CLASS_DRIFT
    assert report["layout"]["generation_detected"] == GEN_UNKNOWN
    assert report["summary"]["drift"] >= 2


def test_drift_inside_canonical_bucket(tmp_path):
    root = tmp_path / "bucket-drift"
    root.mkdir()
    _touch(root / "ledger", directory=True)
    _touch(root / "ledger" / "mac.db")
    _touch(root / "ledger" / "surprise.txt")
    report = audit_mac_home(root)
    by_path = _by_path(report)
    assert by_path["ledger"]["classification"] == CLASS_CANONICAL
    assert by_path["ledger/mac.db"]["classification"] == CLASS_CANONICAL
    assert by_path["ledger/surprise.txt"]["classification"] == CLASS_DRIFT
    assert by_path["ledger/surprise.txt"]["container"] == "ledger"


def test_gateway_reuses_hermes_allow_list(tmp_path):
    root = tmp_path / "gw"
    root.mkdir()
    _touch(root / "gateway", directory=True)
    _touch(root / "gateway" / "SOUL.md")
    _touch(root / "gateway" / "openclaw", directory=True)
    _touch(root / "gateway" / "not-a-hermes-name")
    report = audit_mac_home(root)
    by_path = _by_path(report)
    assert by_path["gateway/SOUL.md"]["classification"] == CLASS_CANONICAL
    assert by_path["gateway/openclaw"]["classification"] == CLASS_CANONICAL
    assert by_path["gateway/not-a-hermes-name"]["classification"] == CLASS_DRIFT


def test_mixed_generation(tmp_path):
    root = tmp_path / "mixed"
    root.mkdir()
    _touch(root / "ledger", directory=True)
    _touch(root / "mac.db")
    report = audit_mac_home(root)
    assert report["layout"]["generation_detected"] == GEN_MIXED


def test_missing_expected_when_nothing_present(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    report = audit_mac_home(root)
    missing = {item["path"] for item in report["missing_expected"]}
    for expected in expected_paths():
        assert expected.relative in missing
        assert all(item["satisfied_by_legacy"] is None for item in report["missing_expected"])


def test_missing_root_handling(tmp_path):
    missing = tmp_path / "does-not-exist"
    report = audit_mac_home(missing)
    assert report["status"] == "missing"
    assert report["root_exists"] is False
    assert report["entries"] == []
    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA
    assert {item["path"] for item in report["missing_expected"]} == {
        item.relative for item in expected_paths()
    }


def test_not_a_directory_root(tmp_path):
    target = tmp_path / "a-file"
    target.write_text("nope", encoding="utf-8")
    report = audit_mac_home(target)
    assert report["status"] == "not_a_directory"
    assert report["root_exists"] is True
    assert report["entries"] == []


def test_unreadable_root(tmp_path):
    root = tmp_path / "locked"
    root.mkdir()
    _touch(root / "mac.db")
    root.chmod(0)
    try:
        report = audit_mac_home(root)
    finally:
        root.chmod(0o700)
    assert report["status"] == "unreadable"
    assert report["entries"] == []


def test_home_kwarg_and_default_mac_paths(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit"
    explicit.mkdir()
    _touch(explicit / "fleets.yaml")
    via_kw = audit_mac_home(home=explicit)
    assert via_kw["root_path"] == str(explicit)
    assert _by_path(via_kw)["fleets.yaml"]["classification"] == CLASS_LEGACY

    relocated = tmp_path / "relocated-mac"
    relocated.mkdir()
    _touch(relocated / "src", directory=True)
    monkeypatch.setenv("MAC_HOME", str(relocated))
    via_default = audit_mac_home()
    assert via_default["root_path"] == str(relocated)
    assert _by_path(via_default)["src"]["canonical_target"] == "toolchain/src"


def test_root_argument_wins_over_home(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _touch(a / "mac.db")
    _touch(b / "weird")
    report = audit_mac_home(a, home=b)
    assert "mac.db" in _by_path(report)
    assert "weird" not in _by_path(report)


def test_read_only_snapshot_unchanged(tmp_path):
    root = tmp_path / "ro"
    root.mkdir()
    _touch(root / "mac.db")
    _touch(root / "journal", directory=True)
    _touch(root / "journal" / "2026-08-19", directory=True)
    _touch(root / "journal" / "2026-08-19" / "SOUL.md")
    _touch(root / "openclaw", directory=True)
    before = _snapshot(root)
    listing_before = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    audit_mac_home(root)
    after = _snapshot(root)
    listing_after = sorted(p.relative_to(root).as_posix() for p in root.rglob("*"))
    assert listing_before == listing_after
    assert before == after


def test_no_hardcoded_home_literals():
    source = Path(mha.__file__).read_text(encoding="utf-8")
    assert "Path.home()" not in source
    assert '".mac"' not in source
    assert "'.mac'" not in source
    assert '".hermes"' not in source
    assert "mac._hermes" not in source


def test_does_not_import_hermes_vendor():
    assert "mac._hermes" not in getattr(mha, "__dict__", {})
    assert not any(name.startswith("_hermes") for name in dir(mha))


def test_symlink_kind_and_canonical_bucket_scan(tmp_path):
    root = tmp_path / "links"
    root.mkdir()
    real = tmp_path / "real-ledger"
    real.mkdir()
    _touch(real / "mac.db")
    (root / "ledger").symlink_to(real)
    report = audit_mac_home(root)
    by_path = _by_path(report)
    assert by_path["ledger"]["kind"] == "symlink"
    assert by_path["ledger"]["classification"] == CLASS_CANONICAL
    assert "ledger/mac.db" in by_path


def test_reserved_duplicate_orphan_keys_stable(tmp_path):
    report = audit_mac_home(tmp_path / "empty-home")
    # missing root — keys must still be present
    assert "duplicates" in report
    assert "orphans" in report
    assert "duplicates" in report["summary"]
    assert "orphans" in report["summary"]


def test_toolchain_and_secrets_legacy(tmp_path):
    root = tmp_path / "more-legacy"
    root.mkdir()
    _touch(root / "venv", directory=True)
    _touch(root / ".env")
    _touch(root / "client-principals.json")
    _touch(root / "hermes-agent", directory=True)
    by_path = _by_path(audit_mac_home(root))
    assert by_path["venv"]["canonical_target"] == "toolchain/venv"
    assert by_path[".env"]["canonical_target"] == "secrets/.env"
    assert by_path["client-principals.json"]["canonical_target"] == (
        "secrets/client-principals.json"
    )
    assert by_path["hermes-agent"]["canonical_target"] == "toolchain/hermes-agent"


def test_inaccessible_child_does_not_raise(tmp_path, monkeypatch):
    root = tmp_path / "ok"
    root.mkdir()
    ghost = root / "ghost"
    _touch(ghost)

    real_lstat = Path.lstat

    def boom(self):
        if self == ghost:
            raise OSError("nope")
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", boom)
    report = audit_mac_home(root)
    assert _by_path(report)["ghost"]["kind"] == "inaccessible"


def test_exists_oserror_treated_as_missing(tmp_path, monkeypatch):
    root = tmp_path / "e"
    root.mkdir()
    real_exists = Path.exists

    def boom(self):
        rel = str(self)
        if rel.endswith("ledger") or rel.endswith("ledger/mac.db"):
            raise OSError("stat failed")
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", boom)
    report = audit_mac_home(root)
    missing = {item["path"] for item in report["missing_expected"]}
    assert "ledger" in missing


def test_status_unreadable_when_is_dir_raises(tmp_path, monkeypatch):
    root = tmp_path / "r"
    root.mkdir()
    real_is_dir = Path.is_dir

    def boom(self):
        if self == root:
            raise OSError("x")
        return real_is_dir(self)

    monkeypatch.setattr(Path, "is_dir", boom)
    report = audit_mac_home(root)
    assert report["status"] == "unreadable"


def test_canonical_buckets_match_spec():
    assert CANONICAL_BUCKETS == frozenset(bucket.name for bucket in UNIFIED_LAYOUT)


def test_string_root_accepted(tmp_path):
    root = tmp_path / "strroot"
    root.mkdir()
    report = audit_mac_home(str(root))
    assert report["status"] == "ok"


def test_empty_tree_summary_counts(tmp_path):
    root = tmp_path / "z"
    root.mkdir()
    report = audit_mac_home(root)
    assert report["summary"]["canonical"] == 0
    assert report["summary"]["legacy_accepted"] == 0
    assert report["summary"]["drift"] == 0
    assert report["summary"]["entry_count"] == 0
    assert report["summary"]["missing_expected"] == len(expected_paths())


def test_does_not_scan_inside_legacy_journal(tmp_path):
    root = tmp_path / "j"
    root.mkdir()
    _touch(root / "journal" / "2026-01-01" / "SOUL.md")
    paths = set(_by_path(audit_mac_home(root)))
    assert "journal" in paths
    assert "journal/2026-01-01" not in paths


def test_runtime_known_extra_qdrant_is_canonical(tmp_path):
    root = tmp_path / "rt"
    root.mkdir()
    _touch(root / "runtime", directory=True)
    _touch(root / "runtime" / "qdrant", directory=True)
    by_path = _by_path(audit_mac_home(root))
    assert by_path["runtime/qdrant"]["classification"] == CLASS_CANONICAL


def test_stat_module_used_for_kinds(tmp_path):
    root = tmp_path / "fifo-home"
    root.mkdir()
    fifo = root / "a-fifo"
    os.mkfifo(fifo)
    report = audit_mac_home(root)
    kind = _by_path(report)["a-fifo"]["kind"]
    assert kind == "other"
    assert stat.S_ISFIFO(fifo.lstat().st_mode)
