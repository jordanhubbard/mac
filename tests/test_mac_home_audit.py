"""Contract tests for the read-only unified-$MAC_HOME auditor.

The audit exists to answer "is this root the layout docs/home-consolidation.md
§4 approved, the pre-migration shape we still run, or something nobody
recognises?" — so the tests are organised the same way: canonical recognition,
legacy acceptance, drift, missing-expected reporting, hostile roots, schema
shape, and the read-only proof.
"""

from __future__ import annotations

import ast
import os
import re
from datetime import datetime
from pathlib import Path

import pytest

from mac import mac_home_audit
from mac.mac_home_audit import (
    CANONICAL,
    DRIFT,
    GENERATION_EMPTY,
    GENERATION_MIXED,
    GENERATION_PRE_MIGRATION,
    GENERATION_UNIFIED,
    GENERATION_UNKNOWN,
    LEGACY_ACCEPTED,
    MAC_HOME_AUDIT_SCHEMA,
    MAC_HOME_LAYOUT,
    ROOT_MISSING,
    ROOT_NOT_A_DIRECTORY,
    ROOT_OK,
    ROOT_UNREADABLE,
    BucketSpec,
    LayoutSpec,
    LegacyEntrySpec,
    audit_mac_home,
    classify_root_entry,
)

_MODULE_PATH = Path(mac_home_audit.__file__)


# --- fixtures ---------------------------------------------------------------


def _make_canonical_root(base: Path) -> Path:
    """A fully migrated root: every §4 bucket with every enumerated entry."""
    root = base / "canonical-home"
    for bucket in MAC_HOME_LAYOUT.buckets:
        (root / bucket.name).mkdir(parents=True, exist_ok=True)
        for entry in bucket.entries:
            target = root / bucket.name / entry
            if entry.endswith((".db", ".env", ".json", ".md", ".yaml")):
                target.write_text("", encoding="utf-8")
            else:
                target.mkdir(exist_ok=True)
    return root


def _make_legacy_root(base: Path) -> Path:
    """Today's pre-Phase-2 flat root, exactly as task/doc describe it."""
    root = base / "legacy-home"
    root.mkdir(parents=True, exist_ok=True)
    for name in ("mac.db", "mac.env", "fleets.yaml"):
        (root / name).write_text("", encoding="utf-8")
    for name in ("openclaw", "journal", "backups"):
        (root / name).mkdir(exist_ok=True)
    return root


def _relatives(report, key):
    return set(report[key])


def _entry(report, relative_path):
    for record in report["entries"]:
        if record["relative_path"] == relative_path:
            return record
    raise AssertionError(
        "no entry %r in %s" % (relative_path, [r["relative_path"] for r in report["entries"]])
    )


def _snapshot(root: Path):
    """Listing + kind + size + mtime_ns of every path under ``root``."""
    seen = {}
    for path in sorted(root.rglob("*")):
        stat = path.lstat()
        seen[str(path.relative_to(root))] = (
            path.is_dir(),
            stat.st_size,
            stat.st_mtime_ns,
            stat.st_mode,
        )
    root_stat = root.lstat()
    seen["."] = (True, root_stat.st_size, root_stat.st_mtime_ns, root_stat.st_mode)
    return seen


# --- canonical layout recognition ------------------------------------------


def test_a_fully_migrated_root_is_entirely_canonical(tmp_path):
    report = audit_mac_home(_make_canonical_root(tmp_path))

    assert report["drift"] == []
    assert report["legacy_accepted"] == []
    assert report["missing_expected"] == []
    assert report["layout_generation"] == GENERATION_UNIFIED
    for bucket in MAC_HOME_LAYOUT.buckets:
        assert bucket.name in report["canonical"]
        for entry in bucket.entries:
            assert "%s/%s" % (bucket.name, entry) in report["canonical"]


def test_every_canonical_path_the_spec_enumerates_is_recognised(tmp_path):
    report = audit_mac_home(_make_canonical_root(tmp_path))
    assert set(MAC_HOME_LAYOUT.canonical_paths()) == _relatives(report, "canonical")


def test_the_six_documented_buckets_are_the_spec(tmp_path):
    assert MAC_HOME_LAYOUT.bucket_names == (
        "ledger",
        "secrets",
        "fleet",
        "runtime",
        "gateway",
        "toolchain",
    )


@pytest.mark.parametrize(
    "bucket, entry",
    [
        ("ledger", "mac.db"),
        ("ledger", "backups"),
        ("ledger", "archive"),
        ("secrets", "mac.env"),
        ("secrets", ".env"),
        ("secrets", "client-principals.json"),
        ("fleet", "fleets.yaml"),
        ("fleet", "specs"),
        ("runtime", "mac-runtime-context.json"),
        ("runtime", "mac-runtime-context.md"),
        ("runtime", "mac-memory-topology.json"),
        ("runtime", "journal"),
        ("gateway", "openclaw"),
        ("toolchain", "src"),
        ("toolchain", "venv"),
        ("toolchain", "bin"),
        ("toolchain", "hermes-agent"),
    ],
)
def test_documented_bucket_contents_are_enumerated_in_the_spec(bucket, entry):
    spec = MAC_HOME_LAYOUT.bucket(bucket)
    assert spec is not None
    assert spec.knows(entry)


def test_canonical_entries_carry_their_bucket_and_generation(tmp_path):
    report = audit_mac_home(_make_canonical_root(tmp_path))
    record = _entry(report, "ledger/mac.db")
    assert record["classification"] == CANONICAL
    assert record["layout_generation"] == GENERATION_UNIFIED
    assert record["bucket"] == "ledger"
    assert record["depth"] == 1
    assert record["kind"] == "file"
    assert record["path"] == str(tmp_path / "canonical-home" / "ledger" / "mac.db")


# --- legacy-accepted classification ----------------------------------------


def test_todays_flat_root_is_accepted_not_drift(tmp_path):
    report = audit_mac_home(_make_legacy_root(tmp_path))

    assert report["drift"] == []
    assert _relatives(report, "legacy_accepted") == {
        "mac.db",
        "mac.env",
        "fleets.yaml",
        "openclaw",
        "journal",
        "backups",
    }
    assert report["layout_generation"] == GENERATION_PRE_MIGRATION


@pytest.mark.parametrize(
    "name, target",
    [
        ("mac.db", "ledger/mac.db"),
        ("backups", "ledger/backups"),
        ("archive", "ledger/archive"),
        ("mac.env", "secrets/mac.env"),
        (".env", "secrets/.env"),
        ("client-principals.json", "secrets/client-principals.json"),
        ("fleets.yaml", "fleet/fleets.yaml"),
        ("journal", "runtime/journal"),
        ("openclaw", "gateway/openclaw"),
        ("src", "toolchain/src"),
        ("venv", "toolchain/venv"),
        ("bin", "toolchain/bin"),
        ("hermes-agent", "toolchain/hermes-agent"),
    ],
)
def test_each_legacy_location_names_its_canonical_target(name, target):
    classified = classify_root_entry(name)
    assert classified["classification"] == LEGACY_ACCEPTED
    assert classified["layout_generation"] == GENERATION_PRE_MIGRATION
    assert classified["canonical_target"] == target
    assert classified["bucket"] == target.split("/", 1)[0]
    assert target in classified["note"]


def test_a_legacy_entry_the_target_layout_does_not_place_is_reported_as_unplaced(tmp_path):
    root = tmp_path / "home"
    (root / "qdrant").mkdir(parents=True)

    report = audit_mac_home(root)

    assert report["legacy_accepted"] == ["qdrant"]
    assert report["unplaced_legacy"] == ["qdrant"]
    assert report["summary"]["unplaced_legacy_count"] == 1
    record = _entry(report, "qdrant")
    assert record["canonical_target"] is None
    assert record["bucket"] is None
    assert "does not enumerate a target" in record["note"]


def test_a_half_migrated_root_reports_a_mixed_generation(tmp_path):
    root = _make_legacy_root(tmp_path)
    (root / "ledger").mkdir()

    report = audit_mac_home(root)

    assert report["layout_generation"] == GENERATION_MIXED
    assert _entry(report, "ledger")["layout_generation"] == GENERATION_UNIFIED
    assert _entry(report, "mac.db")["layout_generation"] == GENERATION_PRE_MIGRATION


# --- drift ------------------------------------------------------------------


def test_an_unknown_top_level_entry_is_drift(tmp_path):
    root = _make_legacy_root(tmp_path)
    (root / "who-put-this-here").mkdir()
    (root / "stray-note.txt").write_text("x", encoding="utf-8")

    report = audit_mac_home(root)

    assert _relatives(report, "drift") == {"who-put-this-here", "stray-note.txt"}
    assert report["summary"]["drift_count"] == 2
    record = _entry(report, "who-put-this-here")
    assert record["layout_generation"] == GENERATION_UNKNOWN
    assert record["bucket"] is None
    assert record["depth"] == 0


def test_an_unknown_entry_inside_a_known_bucket_is_drift(tmp_path):
    root = _make_canonical_root(tmp_path)
    (root / "ledger" / "mac.db.bak").write_text("", encoding="utf-8")
    (root / "gateway" / "dream_logs_old").mkdir()

    report = audit_mac_home(root)

    assert _relatives(report, "drift") == {"ledger/mac.db.bak", "gateway/dream_logs_old"}
    record = _entry(report, "ledger/mac.db.bak")
    assert record["bucket"] == "ledger"
    assert record["depth"] == 1
    assert "ledger bucket" in record["note"]


def test_drift_detection_stops_one_level_below_a_bucket(tmp_path):
    root = _make_canonical_root(tmp_path)
    (root / "gateway" / "sessions" / "whatever").mkdir(parents=True, exist_ok=True)

    report = audit_mac_home(root)

    assert report["drift"] == []
    assert all(record["depth"] <= 1 for record in report["entries"])


def test_a_root_of_only_unknown_entries_has_an_unknown_generation(tmp_path):
    root = tmp_path / "home"
    (root / "nonsense").mkdir(parents=True)

    report = audit_mac_home(root)

    assert report["layout_generation"] == GENERATION_UNKNOWN
    assert report["drift"] == ["nonsense"]


def test_an_empty_root_has_an_empty_generation(tmp_path):
    root = tmp_path / "home"
    root.mkdir()

    report = audit_mac_home(root)

    assert report["layout_generation"] == GENERATION_EMPTY
    assert report["entries"] == []
    assert report["root_status"] == ROOT_OK


def test_the_gateway_allow_list_is_the_gateway_buckets_enumeration():
    gateway = MAC_HOME_LAYOUT.bucket("gateway")
    assert gateway.entries == mac_home_audit.GATEWAY_HOME_ENTRIES
    # The agent-personal tree §4 folds in, plus the already-nested OpenClaw home.
    for expected in ("SOUL.md", "memories", "sessions", "skills", "cron", "dream_logs", "openclaw"):
        assert gateway.knows(expected)


# --- missing expected paths -------------------------------------------------


def test_a_legacy_root_reports_the_canonical_buckets_as_missing(tmp_path):
    report = audit_mac_home(_make_legacy_root(tmp_path))

    missing = {item["relative_path"]: item for item in report["missing_expected"]}
    assert set(missing) == set(MAC_HOME_LAYOUT.bucket_names)
    assert missing["ledger"]["satisfied_by_legacy"] == ["backups", "mac.db"]
    assert missing["gateway"]["satisfied_by_legacy"] == ["openclaw"]
    assert missing["runtime"]["satisfied_by_legacy"] == ["journal"]
    assert missing["toolchain"]["satisfied_by_legacy"] == []
    assert missing["ledger"]["kind"] == "directory"
    assert missing["ledger"]["path"] == str(tmp_path / "legacy-home" / "ledger")


def test_a_present_bucket_reports_only_its_own_missing_entries(tmp_path):
    root = tmp_path / "home"
    (root / "ledger").mkdir(parents=True)
    (root / "ledger" / "mac.db").write_text("", encoding="utf-8")
    (root / "mac.env").write_text("", encoding="utf-8")

    report = audit_mac_home(root)

    missing = {item["relative_path"]: item for item in report["missing_expected"]}
    assert "ledger" not in missing
    assert missing["ledger/backups"]["bucket"] == "ledger"
    assert missing["ledger/archive"]["satisfied_by_legacy"] == []
    assert "ledger/mac.db" not in missing
    assert missing["secrets"]["satisfied_by_legacy"] == ["mac.env"]


def test_a_bucket_level_entry_still_at_the_root_is_named_as_the_standin(tmp_path):
    root = tmp_path / "home"
    (root / "secrets").mkdir(parents=True)
    (root / "mac.env").write_text("", encoding="utf-8")
    (root / ".env").write_text("", encoding="utf-8")

    report = audit_mac_home(root)

    missing = {item["relative_path"]: item for item in report["missing_expected"]}
    assert missing["secrets/mac.env"]["satisfied_by_legacy"] == ["mac.env"]
    assert missing["secrets/.env"]["satisfied_by_legacy"] == [".env"]
    assert missing["secrets/client-principals.json"]["satisfied_by_legacy"] == []


def test_a_bucket_that_is_a_file_is_not_descended_into(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    (root / "ledger").write_text("not a directory", encoding="utf-8")

    report = audit_mac_home(root)

    record = _entry(report, "ledger")
    assert record["classification"] == CANONICAL
    assert record["kind"] == "file"
    assert report["drift"] == []
    # The bucket exists as a name, so §4's contents are reported as missing.
    missing = {item["relative_path"] for item in report["missing_expected"]}
    assert "ledger/mac.db" in missing


def test_a_symlinked_bucket_is_recorded_but_not_followed(tmp_path):
    real = tmp_path / "elsewhere"
    (real / "surprise").mkdir(parents=True)
    root = tmp_path / "home"
    root.mkdir()
    (root / "gateway").symlink_to(real, target_is_directory=True)

    report = audit_mac_home(root)

    assert _entry(report, "gateway")["kind"] == "symlink"
    assert report["drift"] == []


# --- hostile / unreadable roots --------------------------------------------


def test_a_missing_root_is_reported_not_raised(tmp_path):
    report = audit_mac_home(tmp_path / "does-not-exist")

    assert report["root_exists"] is False
    assert report["root_status"] == ROOT_MISSING
    assert report["root_error"] is None
    assert report["entries"] == []
    assert report["missing_expected"] == []
    assert report["layout_generation"] == GENERATION_UNKNOWN
    assert report["summary"]["entry_count"] == 0


def test_a_root_that_is_a_file_is_reported_not_raised(tmp_path):
    target = tmp_path / "mac-home-is-a-file"
    target.write_text("oops", encoding="utf-8")

    report = audit_mac_home(target)

    assert report["root_exists"] is True
    assert report["root_status"] == ROOT_NOT_A_DIRECTORY
    assert report["entries"] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_an_unreadable_root_is_reported_not_raised(tmp_path):
    root = tmp_path / "home"
    (root / "ledger").mkdir(parents=True)
    root.chmod(0o000)
    try:
        report = audit_mac_home(root)
    finally:
        root.chmod(0o700)

    assert report["root_exists"] is True
    assert report["root_status"] == ROOT_UNREADABLE
    assert report["root_error"]
    assert report["entries"] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_an_unreadable_bucket_is_reported_without_losing_the_rest(tmp_path):
    root = tmp_path / "home"
    (root / "ledger").mkdir(parents=True)
    (root / "secrets").mkdir()
    (root / "ledger").chmod(0o000)
    try:
        report = audit_mac_home(root)
    finally:
        (root / "ledger").chmod(0o700)

    assert report["root_status"] == ROOT_OK
    assert [item["relative_path"] for item in report["unreadable_paths"]] == ["ledger"]
    assert report["unreadable_paths"][0]["error"]
    assert report["summary"]["unreadable_path_count"] == 1
    assert "secrets" in report["canonical"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_a_root_whose_parent_denies_access_is_reported_not_raised(tmp_path):
    locked = tmp_path / "locked"
    root = locked / "home"
    root.mkdir(parents=True)
    locked.chmod(0o000)
    try:
        report = audit_mac_home(root)
    finally:
        locked.chmod(0o700)

    assert report["root_status"] == ROOT_UNREADABLE
    assert report["root_exists"] is True
    assert report["root_error"]
    assert report["entries"] == []


@pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory permissions")
def test_an_entry_that_cannot_be_stat_ed_is_reported_as_unreadable(tmp_path):
    # Mode r-- lists names but forbids stat-ing the children, which is exactly
    # the case pathlib's is_dir()/is_file() would silently answer False for.
    root = tmp_path / "home"
    (root / "ledger").mkdir(parents=True)
    root.chmod(0o400)
    try:
        report = audit_mac_home(root)
    finally:
        root.chmod(0o700)

    assert report["root_status"] == ROOT_OK
    assert _entry(report, "ledger")["kind"] == "unreadable"
    assert report["canonical"] == ["ledger"]
    # Unreadable is not a directory we can descend, so no child drift is claimed.
    assert report["drift"] == []


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="platform has no FIFOs")
def test_a_root_entry_that_is_neither_file_dir_nor_symlink_is_other(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    os.mkfifo(root / "a-pipe")

    report = audit_mac_home(root)

    assert _entry(report, "a-pipe")["kind"] == "other"
    assert report["drift"] == ["a-pipe"]


def test_a_broken_symlink_at_the_root_is_classified_without_raising(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    (root / "dangling").symlink_to(tmp_path / "nowhere")

    report = audit_mac_home(root)

    assert _entry(report, "dangling")["kind"] == "symlink"
    assert report["drift"] == ["dangling"]


# --- default root resolution ------------------------------------------------


def test_the_default_root_comes_from_mac_paths(tmp_path, monkeypatch):
    home = tmp_path / "relocated"
    (home / "ledger").mkdir(parents=True)
    monkeypatch.setenv("MAC_HOME", str(home))

    report = audit_mac_home()

    assert report["root_path"] == str(home)
    assert report["canonical"] == ["ledger"]


def test_a_string_root_and_a_tilde_root_are_both_accepted(tmp_path, monkeypatch):
    home = tmp_path / "tilde-home"
    (home / "fleet").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    assert audit_mac_home(str(home))["root_path"] == str(home)
    assert audit_mac_home("~")["root_path"] == str(home)


# --- schema and summary shape ----------------------------------------------


def test_the_schema_string_is_exactly_the_published_one():
    assert MAC_HOME_AUDIT_SCHEMA == "mac.mac_home_audit.v1"


def test_the_report_has_the_documented_top_level_shape(tmp_path):
    report = audit_mac_home(_make_legacy_root(tmp_path))

    assert set(report) == {
        "schema",
        "root_path",
        "audited_at",
        "root_exists",
        "root_status",
        "root_error",
        "layout_generation",
        "entries",
        "canonical",
        "legacy_accepted",
        "drift",
        "unplaced_legacy",
        "missing_expected",
        "unreadable_paths",
        "duplicates",
        "orphans",
        "summary",
    }
    assert report["schema"] == MAC_HOME_AUDIT_SCHEMA


def test_duplicate_and_orphan_keys_are_reserved_and_empty(tmp_path):
    report = audit_mac_home(_make_legacy_root(tmp_path))

    assert report["duplicates"] == []
    assert report["orphans"] == []
    assert report["summary"]["duplicate_count"] == 0
    assert report["summary"]["orphan_count"] == 0


def test_audited_at_is_iso_8601_utc(tmp_path):
    report = audit_mac_home(tmp_path)

    parsed = datetime.fromisoformat(report["audited_at"])
    assert parsed.utcoffset().total_seconds() == 0


def test_the_summary_counts_agree_with_the_lists(tmp_path):
    root = _make_legacy_root(tmp_path)
    (root / "ledger").mkdir()
    (root / "mystery").mkdir()

    report = audit_mac_home(root)
    summary = report["summary"]

    assert summary["entry_count"] == len(report["entries"])
    assert summary["canonical_count"] == len(report["canonical"])
    assert summary["legacy_accepted_count"] == len(report["legacy_accepted"])
    assert summary["drift_count"] == len(report["drift"])
    assert summary["missing_expected_count"] == len(report["missing_expected"])
    assert summary["unplaced_legacy_count"] == len(report["unplaced_legacy"])
    assert summary["unreadable_path_count"] == len(report["unreadable_paths"])
    assert summary["top_level_count"] == 8
    assert summary["buckets_present"] == ["ledger"]
    assert summary["buckets_missing"] == [
        "secrets",
        "fleet",
        "runtime",
        "gateway",
        "toolchain",
    ]
    assert (
        summary["canonical_count"]
        + summary["legacy_accepted_count"]
        + summary["drift_count"]
        == summary["entry_count"]
    )


def test_every_entry_record_has_the_same_keys(tmp_path):
    report = audit_mac_home(_make_canonical_root(tmp_path))

    expected = {
        "name",
        "relative_path",
        "path",
        "depth",
        "kind",
        "classification",
        "layout_generation",
        "bucket",
        "canonical_target",
        "note",
    }
    assert report["entries"]
    for record in report["entries"]:
        assert set(record) == expected
        assert record["classification"] in {CANONICAL, LEGACY_ACCEPTED, DRIFT}


def test_the_report_is_json_serialisable(tmp_path):
    import json

    report = audit_mac_home(_make_legacy_root(tmp_path))
    assert json.loads(json.dumps(report)) == report


# --- spec-object behaviour --------------------------------------------------


def test_an_unknown_bucket_or_legacy_name_resolves_to_none():
    assert MAC_HOME_LAYOUT.bucket("nope") is None
    assert MAC_HOME_LAYOUT.legacy("nope") is None


def test_legacy_sources_for_finds_every_root_entry_targeting_a_path():
    assert MAC_HOME_LAYOUT.legacy_sources_for("ledger/mac.db") == ("mac.db",)
    assert MAC_HOME_LAYOUT.legacy_sources_for("nothing/here") == ()


def test_the_spec_is_data_a_caller_can_substitute(tmp_path):
    custom = LayoutSpec(
        buckets=(BucketSpec(name="only", purpose="p", entries=("kept",)),),
        legacy_entries=(LegacyEntrySpec("old", "only/kept", "test"),),
    )
    root = tmp_path / "home"
    (root / "only" / "kept").mkdir(parents=True)
    (root / "only" / "extra").mkdir()
    (root / "old").mkdir()
    (root / "ledger").mkdir()

    report = audit_mac_home(root, layout=custom)

    assert report["canonical"] == ["only", "only/kept"]
    assert report["legacy_accepted"] == ["old"]
    assert report["drift"] == ["ledger", "only/extra"]
    assert report["missing_expected"] == []


def test_a_bucket_without_enumerated_contents_is_opaque(tmp_path):
    custom = LayoutSpec(
        buckets=(BucketSpec(name="opaque", purpose="p"),),
        legacy_entries=(),
    )
    root = tmp_path / "home"
    (root / "opaque" / "anything").mkdir(parents=True)

    report = audit_mac_home(root, layout=custom)

    assert report["canonical"] == ["opaque"]
    assert report["drift"] == []
    assert report["missing_expected"] == []


def test_every_legacy_target_names_a_path_the_canonical_spec_enumerates():
    canonical = set(MAC_HOME_LAYOUT.canonical_paths())
    for spec in MAC_HOME_LAYOUT.legacy_entries:
        if spec.placed:
            assert spec.canonical_target in canonical, spec.name


def test_no_legacy_name_collides_with_a_bucket_name():
    buckets = set(MAC_HOME_LAYOUT.bucket_names)
    for spec in MAC_HOME_LAYOUT.legacy_entries:
        assert spec.name not in buckets


# --- read-only proof --------------------------------------------------------


def test_the_audit_does_not_modify_the_tree_it_reads(tmp_path):
    root = _make_canonical_root(tmp_path)
    (root / "stray").mkdir()
    (root / "ledger" / "unexpected.db").write_text("x", encoding="utf-8")

    before = _snapshot(root)
    report = audit_mac_home(root)
    after = _snapshot(root)

    assert report["drift"]  # the audit really did look at the tree
    assert before == after


def test_auditing_a_missing_root_does_not_create_it(tmp_path):
    absent = tmp_path / "not-there"

    audit_mac_home(absent)

    assert not absent.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == []


def test_the_module_contains_no_mutating_filesystem_calls():
    """Static proof to back the runtime snapshot: no write-shaped call exists."""
    forbidden = {
        "mkdir",
        "makedirs",
        "touch",
        "write_text",
        "write_bytes",
        "chmod",
        "unlink",
        "rmdir",
        "remove",
        "rename",
        "replace",
        "symlink_to",
        "hardlink_to",
        "rmtree",
        "copy",
        "copy2",
        "copytree",
        "move",
        "open",
    }
    tree = ast.parse(_MODULE_PATH.read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute):
                called.add(func.attr)
            elif isinstance(func, ast.Name):
                called.add(func.id)
    assert not called & forbidden, sorted(called & forbidden)


def test_the_module_does_not_import_the_vendored_hermes_runtime():
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert "_hermes" not in source


def test_the_module_names_no_home_literal():
    """The same ratchet tests/test_mac_paths_no_hardcode.py enforces repo-wide."""
    source = _MODULE_PATH.read_text(encoding="utf-8")
    assert not re.search(r"""home\(\)\s*/\s*["']\.(mac|hermes)["']""", source)
    assert "mac_paths.mac_home()" in source
