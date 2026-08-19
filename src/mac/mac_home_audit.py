"""Strictly read-only auditor of the unified ``$MAC_HOME`` root.

This module models the canonical unified layout from
``docs/home-consolidation.md`` §4 as *data* (a declarative spec) rather than a
pile of ad-hoc conditionals, then walks the on-disk root and classifies every
observed entry against that spec. It is the generalized successor to the
retired ``src/mac/hermes_home_audit.py`` (deleted as dead code in
task_1db5aa70; see docs/home-consolidation.md §5 "Cross-cutting"), which audited
only the legacy ``~/.hermes`` gateway home.

Design contract:
  * **Read-only.** The audit never creates, writes, ``chmod``s, or deletes
    anything. It only ``stat``s and ``listdir``s. A missing or unreadable root
    is reported in ``root_exists`` / ``status`` fields, never raised.
  * **Migration-aware.** Because the fleet is mid-migration (Phase 0/1 have
    landed, Phase 2 relocation has not), the spec accepts BOTH the canonical
    Phase-2 target layout AND today's flat pre-Phase-2 root (``mac.db``,
    ``mac.env``, ``fleets.yaml``, ``openclaw/``, ``journal/``, ``backups/`` …
    directly at the root). Every observed entry is classified as one of
    ``canonical``, ``legacy_accepted`` (a recognised pre-migration location,
    with its canonical target named), or ``drift`` (unknown).
  * **Root resolution via mac.mac_paths only.** No new host-home literals
    appear here; every root is resolved through :mod:`mac.mac_paths`.
    Callers may pass explicit ``root`` / ``gateway_root`` / ``openclaw_root``
    paths for testability.
  * **No imports from the vendored gateway snapshot package.**

The report schema is ``mac.mac_home_audit.v1`` (:data:`MAC_HOME_AUDIT_SCHEMA`).
Keys for ``duplicates`` and ``orphans`` are reserved (always present, populated
by the sibling task) so the schema is stable across the split.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mac import mac_paths

__all__ = [
    "MAC_HOME_AUDIT_SCHEMA",
    "CANONICAL_BUCKETS",
    "TopLevelSpec",
    "MAC_HOME_SPEC",
    "audit_mac_home",
]

MAC_HOME_AUDIT_SCHEMA = "mac.mac_home_audit.v1"

# Classification generations. Ordered from best (target layout) to worst.
CANONICAL = "canonical"
LEGACY_ACCEPTED = "legacy_accepted"
DRIFT = "drift"


@dataclass(frozen=True)
class ExpectedEntry:
    """One enumerated member of a container directory in the target layout.

    ``name`` is the on-disk basename; ``kind`` is ``"dir"`` or ``"file"``;
    ``required`` marks entries whose absence is reported as a missing expected
    path (optional entries are simply not flagged when absent).
    """

    name: str
    kind: str = "dir"
    required: bool = False


@dataclass(frozen=True)
class TopLevelSpec:
    """A canonical top-level bucket (``ledger/``, ``secrets/`` …).

    ``children`` enumerates the expected members one level deeper, which drives
    both the missing-expected-path report and the deeper non-standard-entry
    detection (the same technique the retired hermes auditor used for the
    gateway home).
    """

    name: str
    kind: str = "dir"
    required: bool = False
    children: Tuple[ExpectedEntry, ...] = ()


@dataclass(frozen=True)
class LegacyEntry:
    """A recognised pre-Phase-2 location that lives directly at the root today.

    ``canonical_target`` names where the same datum lands in the target layout,
    so the report can tell an operator exactly what a legacy entry maps to.
    """

    name: str
    kind: str
    canonical_target: str


# --- Canonical target layout (docs/home-consolidation.md §4) ----------------
#
# $MAC_HOME
# ├── ledger/    mac.db, backups, archive
# ├── secrets/   mac.env, .env, client-principals.json
# ├── fleet/     fleets.yaml, specs
# ├── runtime/   mac-runtime-context.*, mac-memory-topology.json, journal/
# ├── gateway/   (today's ~/.hermes: SOUL, memory, sessions, skills, cron, …)
# │   └── openclaw/
# └── toolchain/ src, venv, bin, hermes-agent

CANONICAL_BUCKETS: Tuple[TopLevelSpec, ...] = (
    TopLevelSpec(
        name="ledger",
        required=True,
        children=(
            ExpectedEntry("mac.db", kind="file", required=True),
            ExpectedEntry("backups", kind="dir"),
            ExpectedEntry("archive", kind="dir"),
        ),
    ),
    TopLevelSpec(
        name="secrets",
        required=True,
        children=(
            ExpectedEntry("mac.env", kind="file", required=True),
            ExpectedEntry(".env", kind="file"),
            ExpectedEntry("client-principals.json", kind="file"),
        ),
    ),
    TopLevelSpec(
        name="fleet",
        required=True,
        children=(
            ExpectedEntry("fleets.yaml", kind="file", required=True),
            ExpectedEntry("specs", kind="dir"),
        ),
    ),
    TopLevelSpec(
        name="runtime",
        required=True,
        children=(
            ExpectedEntry("mac-runtime-context.json", kind="file"),
            ExpectedEntry("mac-runtime-context.md", kind="file"),
            ExpectedEntry("mac-memory-topology.json", kind="file"),
            ExpectedEntry("journal", kind="dir"),
        ),
    ),
    TopLevelSpec(
        name="gateway",
        required=False,
        children=(
            ExpectedEntry("openclaw", kind="dir"),
        ),
    ),
    TopLevelSpec(
        name="toolchain",
        required=False,
        children=(
            ExpectedEntry("src", kind="dir"),
            ExpectedEntry("venv", kind="dir"),
            ExpectedEntry("bin", kind="dir"),
            ExpectedEntry("hermes-agent", kind="dir"),
        ),
    ),
)


# --- Legacy pre-Phase-2 root shape (accepted, not drift) --------------------
#
# The current on-disk root is flat: the datums that §4 nests under buckets live
# directly at ``$MAC_HOME``. Each is accepted (never flagged as drift) and
# reports the canonical bucket path it will move to in Phase 2.

CANONICAL_LEGACY_ENTRIES: Tuple[LegacyEntry, ...] = (
    LegacyEntry("mac.db", "file", "ledger/mac.db"),
    LegacyEntry("mac.db-wal", "file", "ledger/mac.db-wal"),
    LegacyEntry("mac.db-shm", "file", "ledger/mac.db-shm"),
    LegacyEntry("backups", "dir", "ledger/backups"),
    LegacyEntry("archive", "dir", "ledger/archive"),
    LegacyEntry("mac.env", "file", "secrets/mac.env"),
    LegacyEntry(".env", "file", "secrets/.env"),
    LegacyEntry("client-principals.json", "file", "secrets/client-principals.json"),
    LegacyEntry("fleets.yaml", "file", "fleet/fleets.yaml"),
    LegacyEntry("specs", "dir", "fleet/specs"),
    LegacyEntry("mac-runtime-context.json", "file", "runtime/mac-runtime-context.json"),
    LegacyEntry("mac-runtime-context.md", "file", "runtime/mac-runtime-context.md"),
    LegacyEntry("mac-memory-topology.json", "file", "runtime/mac-memory-topology.json"),
    LegacyEntry("journal", "dir", "runtime/journal"),
    LegacyEntry("openclaw", "dir", "gateway/openclaw"),
    LegacyEntry("qdrant", "dir", "runtime/qdrant"),
    LegacyEntry("src", "dir", "toolchain/src"),
    LegacyEntry("venv", "dir", "toolchain/venv"),
    LegacyEntry("bin", "dir", "toolchain/bin"),
    LegacyEntry("hermes-agent", "dir", "toolchain/hermes-agent"),
)


@dataclass(frozen=True)
class MacHomeSpec:
    """The complete declarative spec the detectors and tests consume."""

    buckets: Tuple[TopLevelSpec, ...] = CANONICAL_BUCKETS
    legacy_entries: Tuple[LegacyEntry, ...] = CANONICAL_LEGACY_ENTRIES

    def bucket_names(self) -> frozenset[str]:
        return frozenset(bucket.name for bucket in self.buckets)

    def legacy_names(self) -> frozenset[str]:
        return frozenset(entry.name for entry in self.legacy_entries)

    def bucket(self, name: str) -> Optional[TopLevelSpec]:
        for bucket in self.buckets:
            if bucket.name == name:
                return bucket
        return None

    def legacy(self, name: str) -> Optional[LegacyEntry]:
        for entry in self.legacy_entries:
            if entry.name == name:
                return entry
        return None


MAC_HOME_SPEC = MacHomeSpec()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _entry_kind(path: Path) -> str:
    """Classify a path as ``dir`` / ``file`` / ``symlink`` / ``other``.

    Symlinks are reported as ``symlink`` because the target layout uses
    ``~/.hermes`` → ``$MAC_HOME/gateway`` compatibility symlinks; a caller may
    want to see them without following. ``os.lstat`` never follows.
    """

    try:
        st = os.lstat(path)
    except OSError:
        return "missing"
    import stat as _stat

    mode = st.st_mode
    if _stat.S_ISLNK(mode):
        return "symlink"
    if _stat.S_ISDIR(mode):
        return "dir"
    if _stat.S_ISREG(mode):
        return "file"
    return "other"


def _kind_matches(expected: str, observed: str) -> bool:
    if observed == "symlink":
        # A symlink can stand in for either a file or a dir target.
        return True
    return expected == observed


def _list_dir(path: Path) -> Optional[List[str]]:
    """Return sorted child names, or ``None`` if the dir cannot be read."""

    try:
        return sorted(os.listdir(path))
    except OSError:
        return None


def _classify_top_level(
    spec: MacHomeSpec, name: str, observed_kind: str
) -> Tuple[str, Dict[str, Any]]:
    """Classify a single top-level entry. Returns ``(generation, detail)``."""

    bucket = spec.bucket(name)
    if bucket is not None and _kind_matches(bucket.kind, observed_kind):
        return CANONICAL, {"canonical_target": name}

    legacy = spec.legacy(name)
    if legacy is not None and _kind_matches(legacy.kind, observed_kind):
        return LEGACY_ACCEPTED, {"canonical_target": legacy.canonical_target}

    return DRIFT, {"canonical_target": None}


def _audit_container_children(
    bucket: TopLevelSpec, container: Path
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Audit one level deeper inside a well-known canonical container dir.

    Returns ``(non_standard, missing)`` where ``non_standard`` lists observed
    children not enumerated by the bucket spec (deeper drift) and ``missing``
    lists required children that are absent.
    """

    expected_by_name = {child.name: child for child in bucket.children}
    observed = _list_dir(container)
    non_standard: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []

    if observed is None:
        # Unreadable container: cannot enumerate. Report nothing rather than
        # guess; the top-level entry itself is still recorded by the caller.
        return non_standard, missing

    observed_set = set(observed)
    for child_name in observed:
        if child_name not in expected_by_name:
            child_path = container / child_name
            non_standard.append(
                {
                    "path": f"{bucket.name}/{child_name}",
                    "kind": _entry_kind(child_path),
                }
            )

    for child in bucket.children:
        if child.required and child.name not in observed_set:
            missing.append(
                {
                    "path": f"{bucket.name}/{child.name}",
                    "kind": child.kind,
                }
            )

    return non_standard, missing


def _resolve_root(
    explicit: Optional[Any], resolver
) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    return resolver()


def audit_mac_home(
    root: Optional[Any] = None,
    *,
    gateway_root: Optional[Any] = None,
    openclaw_root: Optional[Any] = None,
    spec: MacHomeSpec = MAC_HOME_SPEC,
) -> Dict[str, Any]:
    """Audit the unified ``$MAC_HOME`` root and return a report dict.

    Args:
        root: Explicit root path (for testability). Defaults to
            ``mac_paths.mac_home()``.
        gateway_root: Explicit gateway home (default ``mac_paths.gateway_home()``);
            recorded for reference only — the gateway subtree is audited by the
            sibling hermes-oriented tooling, not enumerated here.
        openclaw_root: Explicit openclaw home (default
            ``mac_paths.openclaw_home()``); recorded for reference only.
        spec: The declarative layout spec to audit against.

    The result always contains the reserved ``duplicates`` / ``orphans`` keys so
    the ``mac.mac_home_audit.v1`` schema is stable while the sibling task fills
    them in. A missing or unreadable root is reported via ``root_exists`` and
    ``status``; this function never raises for filesystem conditions.
    """

    resolved_root = _resolve_root(root, mac_paths.mac_home)
    resolved_gateway = _resolve_root(gateway_root, mac_paths.gateway_home)
    resolved_openclaw = _resolve_root(openclaw_root, mac_paths.openclaw_home)

    report: Dict[str, Any] = {
        "schema": MAC_HOME_AUDIT_SCHEMA,
        "root_path": str(resolved_root),
        "gateway_home": str(resolved_gateway),
        "openclaw_home": str(resolved_openclaw),
        "audited_at": _utc_now(),
        "root_exists": False,
        "status": "ok",
        "entries": [],
        "non_standard_deeper": [],
        "missing_expected": [],
        # Reserved for the sibling duplicate/orphan task; stable schema.
        "duplicates": [],
        "orphans": [],
        "summary": {
            "canonical": 0,
            "legacy_accepted": 0,
            "drift": 0,
            "non_standard_deeper": 0,
            "missing_expected": 0,
        },
    }

    root_kind = _entry_kind(resolved_root)
    if root_kind == "missing":
        report["status"] = "root_missing"
        return report
    if root_kind != "dir":
        # A non-directory at the root path is drift we cannot walk.
        report["status"] = "root_not_a_directory"
        return report

    report["root_exists"] = True

    top_level = _list_dir(resolved_root)
    if top_level is None:
        report["status"] = "root_unreadable"
        return report

    entries: List[Dict[str, Any]] = []
    non_standard_deeper: List[Dict[str, Any]] = []
    missing_expected: List[Dict[str, Any]] = []

    for name in top_level:
        entry_path = resolved_root / name
        observed_kind = _entry_kind(entry_path)
        generation, detail = _classify_top_level(spec, name, observed_kind)
        entries.append(
            {
                "name": name,
                "kind": observed_kind,
                "generation": generation,
                "canonical_target": detail["canonical_target"],
            }
        )

        # One level deeper: only well-known canonical container dirs are
        # enumerated (same technique the retired hermes auditor used).
        if generation == CANONICAL and observed_kind == "dir":
            bucket = spec.bucket(name)
            if bucket is not None and bucket.children:
                deeper, sub_missing = _audit_container_children(bucket, entry_path)
                non_standard_deeper.extend(deeper)
                missing_expected.extend(sub_missing)

    # Missing top-level buckets: only meaningful once relocation begins. A
    # required bucket that is absent AND has no accepted legacy stand-in at the
    # root is reported as a missing expected path.
    present_names = {entry["name"] for entry in entries}
    for bucket in spec.buckets:
        if not bucket.required:
            continue
        if bucket.name in present_names:
            continue
        # Is a legacy stand-in for this bucket's datums present at the root?
        legacy_present = any(
            legacy.canonical_target.split("/", 1)[0] == bucket.name
            and legacy.name in present_names
            for legacy in spec.legacy_entries
        )
        if legacy_present:
            continue
        missing_expected.append({"path": bucket.name, "kind": bucket.kind})

    report["entries"] = entries
    report["non_standard_deeper"] = non_standard_deeper
    report["missing_expected"] = missing_expected

    summary = report["summary"]
    for entry in entries:
        summary[entry["generation"]] += 1
    summary["non_standard_deeper"] = len(non_standard_deeper)
    summary["missing_expected"] = len(missing_expected)

    return report
