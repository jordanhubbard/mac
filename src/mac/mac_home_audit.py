"""Strictly read-only auditor of the unified MAC home root.

``docs/home-consolidation.md`` §4 approves a single authoritative root with six
top-level buckets (``ledger/``, ``secrets/``, ``fleet/``, ``runtime/``,
``gateway/`` and ``toolchain/``).  The fleet is mid-migration, so the root on a
live host still has the *pre-migration* flat shape: ``mac.db``, ``mac.env``,
``fleets.yaml``, ``openclaw/``, ``journal/`` and ``backups/`` sit directly at
the root.  An auditor that only knew the target layout would report an entirely
healthy host as broken, so this module models **both** generations as one
declarative spec and classifies every observed entry as:

``canonical``
    it is where §4 says it belongs;
``legacy_accepted``
    it is a recognised pre-migration location, reported together with the
    canonical path the migration will move it to (``canonical_target``);
``drift``
    neither generation knows about it — the thing this audit exists to surface.

The spec is data, not conditionals, so the sibling duplicate-datum / orphan
detectors and the tests consume exactly the rules this module enforces.  The
gateway allow-list (``GATEWAY_HOME_ENTRIES``) is the generalisation of the
per-home allow-list the deleted ``src/mac/hermes_home_audit.py`` carried; that
module and its tests no longer exist in this tree (removed as dead code, see
``docs/home-consolidation.md`` §5 "Cross-cutting"), so the allow-list lives
here as reusable spec data rather than being copied into a second module.

Two invariants this module is built around:

* **Read-only.** Nothing here creates, writes, chmods or deletes.  The only
  filesystem calls are ``iterdir``/``stat``-class reads.  A caller may act on
  the report; the audit never acts on the tree.
* **Never raises on a hostile root.** A missing, unreadable or non-directory
  root is reported in ``root_status`` with an empty entry set, because an
  auditor that explodes on the tree it was pointed at is useless exactly when
  it is needed.

Every root is resolved through :mod:`mac.mac_paths` — this module never names
a home literal (``tests/test_mac_paths_no_hardcode.py`` enforces that).

Duplicate-datum and orphan detection are a sibling task; their report keys are
reserved here (always present, always empty) so the schema is stable before
they land.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG
from typing import Any, Dict, List, Optional, Tuple

from mac import mac_paths

MAC_HOME_AUDIT_SCHEMA = "mac.mac_home_audit.v1"

# --- Classifications -------------------------------------------------------

CANONICAL = "canonical"
LEGACY_ACCEPTED = "legacy_accepted"
DRIFT = "drift"

# Which generation of the layout an entry belongs to.
GENERATION_UNIFIED = "unified"
GENERATION_PRE_MIGRATION = "pre_migration"
GENERATION_UNKNOWN = "unknown"
GENERATION_MIXED = "mixed"
GENERATION_EMPTY = "empty"

_GENERATION_FOR_CLASSIFICATION = {
    CANONICAL: GENERATION_UNIFIED,
    LEGACY_ACCEPTED: GENERATION_PRE_MIGRATION,
    DRIFT: GENERATION_UNKNOWN,
}

# --- Root status -----------------------------------------------------------

ROOT_OK = "ok"
ROOT_MISSING = "missing"
ROOT_NOT_A_DIRECTORY = "not_a_directory"
ROOT_UNREADABLE = "unreadable"


# --- Declarative layout spec ------------------------------------------------


@dataclass(frozen=True)
class BucketSpec:
    """One top-level bucket of the §4 target layout and its expected contents.

    ``entries`` is the enumerated allow-list for the bucket's *immediate*
    children: anything else found one level inside is drift.  An empty
    ``entries`` means "contents not enumerated by §4", and the bucket is then
    treated as opaque rather than as a bucket where everything is drift.
    """

    name: str
    purpose: str
    entries: Tuple[str, ...] = ()

    def knows(self, child: str) -> bool:
        return child in self.entries

    @property
    def enumerated(self) -> bool:
        return bool(self.entries)


@dataclass(frozen=True)
class LegacyEntrySpec:
    """A recognised pre-migration location that still sits at the root.

    ``canonical_target`` is the §4 path the migration moves this entry to, or
    ``None`` when §4 does not enumerate a home for it.  ``None`` is not a
    shrug: it names a real gap in the plan (the entry exists on every host
    today but the target layout never says where it goes), and the report
    surfaces those separately so the migration can decide rather than guess.
    """

    name: str
    canonical_target: Optional[str]
    resolver: str

    @property
    def placed(self) -> bool:
        return self.canonical_target is not None


# The gateway home's own allow-list.  ~/.hermes becomes $MAC_HOME/gateway in
# Phase 2 (docs §4), bringing the agent-personal tree with it; OpenClaw's home
# is already nested here as ``openclaw``.  This is the reusable generalisation
# of the per-home allow-list the removed hermes_home_audit module carried.
GATEWAY_HOME_ENTRIES: Tuple[str, ...] = (
    ".env",
    "SOUL.md",
    "USER.md",
    "MEMORY.md",
    "auth.json",
    "config.yaml",
    "cron",
    "dream_logs",
    "logs",
    "memories",
    "mood",
    "openclaw",
    "plugins",
    "scripts",
    "sessions",
    "skills",
    "state.db",
)


@dataclass(frozen=True)
class LayoutSpec:
    """The complete two-generation layout model the audit is driven by."""

    buckets: Tuple[BucketSpec, ...]
    legacy_entries: Tuple[LegacyEntrySpec, ...]

    def bucket(self, name: str) -> Optional[BucketSpec]:
        for spec in self.buckets:
            if spec.name == name:
                return spec
        return None

    def legacy(self, name: str) -> Optional[LegacyEntrySpec]:
        for spec in self.legacy_entries:
            if spec.name == name:
                return spec
        return None

    @property
    def bucket_names(self) -> Tuple[str, ...]:
        return tuple(spec.name for spec in self.buckets)

    def canonical_paths(self) -> Tuple[str, ...]:
        """Every path §4 enumerates, as ``bucket`` and ``bucket/entry`` strings."""
        paths: List[str] = []
        for spec in self.buckets:
            paths.append(spec.name)
            paths.extend("%s/%s" % (spec.name, entry) for entry in spec.entries)
        return tuple(paths)

    def legacy_sources_for(self, canonical_target: str) -> Tuple[str, ...]:
        """Root-level legacy names whose migration target is ``canonical_target``."""
        return tuple(
            spec.name
            for spec in self.legacy_entries
            if spec.canonical_target == canonical_target
        )


# docs/home-consolidation.md §4 — the approved single authoritative root.
MAC_HOME_LAYOUT = LayoutSpec(
    buckets=(
        BucketSpec(
            name="ledger",
            purpose="Task ledger database, its backups and its archive.",
            entries=("mac.db", "backups", "archive"),
        ),
        BucketSpec(
            name="secrets",
            purpose="The single secret source (0700), hub and client scoped.",
            entries=("mac.env", ".env", "client-principals.json"),
        ),
        BucketSpec(
            name="fleet",
            purpose="Fleet registry and fleet specifications.",
            entries=("fleets.yaml", "specs"),
        ),
        BucketSpec(
            name="runtime",
            purpose="Control-plane runtime context, memory topology and journal.",
            entries=(
                "mac-runtime-context.json",
                "mac-runtime-context.md",
                "mac-memory-topology.json",
                "journal",
            ),
        ),
        BucketSpec(
            name="gateway",
            purpose="The agent-personal gateway home (0700); OpenClaw nested.",
            entries=GATEWAY_HOME_ENTRIES,
        ),
        BucketSpec(
            name="toolchain",
            purpose="Installed source, virtualenv, launchers and agent runtime.",
            entries=("src", "venv", "bin", "hermes-agent"),
        ),
    ),
    # Everything below is a location that exists at the root *today*.  Each is
    # grounded in a resolver in mac.mac_paths or a first-party call site, named
    # in ``resolver`` so a reader can check the claim rather than trust it.
    legacy_entries=(
        LegacyEntrySpec("mac.db", "ledger/mac.db", "mac_paths.ledger_db"),
        LegacyEntrySpec("backups", "ledger/backups", "mac_paths.backups_dir"),
        LegacyEntrySpec("archive", "ledger/archive", "mac_paths.archive_dir"),
        LegacyEntrySpec("mac.env", "secrets/mac.env", "mac_paths.mac_env_file"),
        LegacyEntrySpec(".env", "secrets/.env", "mac_paths.deploy_env_file"),
        LegacyEntrySpec(
            "client-principals.json",
            "secrets/client-principals.json",
            "client_principals.principals_path",
        ),
        LegacyEntrySpec("fleets.yaml", "fleet/fleets.yaml", "mac_paths.fleets_config"),
        LegacyEntrySpec("journal", "runtime/journal", "mac_paths.journal_dir"),
        LegacyEntrySpec("openclaw", "gateway/openclaw", "mac_paths.openclaw_home"),
        LegacyEntrySpec("src", "toolchain/src", "worker.repository_checkout_root"),
        LegacyEntrySpec("venv", "toolchain/venv", "worker_runtime_deps.interpreter"),
        LegacyEntrySpec("bin", "toolchain/bin", "executor_sandbox.host_bin_dir"),
        LegacyEntrySpec("hermes-agent", "toolchain/hermes-agent", "docs §1"),
        # Recognised today, but §4 does not enumerate a home for them.  Left
        # unplaced deliberately — see LegacyEntrySpec.canonical_target.
        LegacyEntrySpec("qdrant", None, "docs §1"),
        LegacyEntrySpec("openshell", None, "openshell_supervisor.agent_policy_path"),
        LegacyEntrySpec("openshell-policy.yaml", None, "executor_sandbox.deployed_policy"),
        LegacyEntrySpec("clients", None, "client_profiles.profiles_dir"),
        LegacyEntrySpec("credentials", None, "client_profiles.credentials_dir"),
        LegacyEntrySpec("sessions", None, "client_login.sessions_dir"),
        LegacyEntrySpec("ssh", None, "client_login.known_hosts_path"),
        LegacyEntrySpec("public-artifacts", None, "webdav_server.--root"),
        LegacyEntrySpec("agent-workspaces", None, "deploy_env.worker_workspace_default"),
        LegacyEntrySpec("agent-footprint.json", None, "worker_runtime_deps.footprint"),
    ),
)


# --- Filesystem reads (the only ones in this module) -----------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _describe_error(exc: OSError) -> str:
    return "%s: %s" % (type(exc).__name__, exc.strerror or exc)


def _list_names(path: Path) -> Tuple[List[str], Optional[str]]:
    """Sorted child names of ``path``; never raises, reports the error instead."""
    try:
        return sorted(child.name for child in path.iterdir()), None
    except OSError as exc:
        return [], _describe_error(exc)


def _kind_of(path: Path) -> str:
    """Kind of ``path`` without following symlinks.

    ``lstat`` rather than ``Path.is_dir()`` on purpose: the pathlib predicates
    swallow ``OSError`` and answer ``False``, which would silently report an
    entry we were not permitted to stat (a root readable but not searchable —
    mode ``r--``) as if it were a plain file.
    """
    try:
        info = path.lstat()
    except OSError:
        return "unreadable"
    if S_ISLNK(info.st_mode):
        return "symlink"
    if S_ISDIR(info.st_mode):
        return "directory"
    if S_ISREG(info.st_mode):
        return "file"
    return "other"


def _root_status(root: Path) -> Tuple[str, Optional[str]]:
    try:
        info = root.stat()
    except FileNotFoundError:
        return ROOT_MISSING, None
    except OSError as exc:
        return ROOT_UNREADABLE, _describe_error(exc)
    if not S_ISDIR(info.st_mode):
        return ROOT_NOT_A_DIRECTORY, None
    return ROOT_OK, None


# --- Classification --------------------------------------------------------


def classify_root_entry(
    name: str, layout: LayoutSpec = MAC_HOME_LAYOUT
) -> Dict[str, Any]:
    """Classify one top-level entry name against both layout generations.

    Pure and filesystem-free, so the sibling detectors and the tests can reuse
    the exact rules the audit applies.
    """
    bucket = layout.bucket(name)
    if bucket is not None:
        return {
            "classification": CANONICAL,
            "layout_generation": GENERATION_UNIFIED,
            "bucket": bucket.name,
            "canonical_target": None,
            "note": bucket.purpose,
        }
    legacy = layout.legacy(name)
    if legacy is not None:
        if legacy.placed:
            note = "Pre-migration location (%s); §4 target is %s." % (
                legacy.resolver,
                legacy.canonical_target,
            )
        else:
            note = (
                "Pre-migration location (%s); §4 does not enumerate a target "
                "for it yet." % legacy.resolver
            )
        return {
            "classification": LEGACY_ACCEPTED,
            "layout_generation": GENERATION_PRE_MIGRATION,
            "bucket": (
                legacy.canonical_target.split("/", 1)[0] if legacy.placed else None
            ),
            "canonical_target": legacy.canonical_target,
            "note": note,
        }
    return {
        "classification": DRIFT,
        "layout_generation": GENERATION_UNKNOWN,
        "bucket": None,
        "canonical_target": None,
        "note": "Not part of the target layout or of any recognised pre-migration shape.",
    }


def _classify_bucket_child(bucket: BucketSpec, child: str) -> Dict[str, Any]:
    if bucket.knows(child):
        return {
            "classification": CANONICAL,
            "layout_generation": GENERATION_UNIFIED,
            "bucket": bucket.name,
            "canonical_target": None,
            "note": "Enumerated content of the %s bucket." % bucket.name,
        }
    return {
        "classification": DRIFT,
        "layout_generation": GENERATION_UNKNOWN,
        "bucket": bucket.name,
        "canonical_target": None,
        "note": "Not an enumerated entry of the %s bucket." % bucket.name,
    }


def _entry_record(
    root: Path, relative: str, depth: int, classification: Dict[str, Any]
) -> Dict[str, Any]:
    path = root / relative
    record = {
        "name": relative.rsplit("/", 1)[-1],
        "relative_path": relative,
        "path": str(path),
        "depth": depth,
        "kind": _kind_of(path),
    }
    record.update(classification)
    return record


# --- The audit -------------------------------------------------------------


def _overall_generation(records: List[Dict[str, Any]]) -> str:
    if not records:
        return GENERATION_EMPTY
    generations = {record["layout_generation"] for record in records}
    unified = GENERATION_UNIFIED in generations
    legacy = GENERATION_PRE_MIGRATION in generations
    if unified and legacy:
        return GENERATION_MIXED
    if unified:
        return GENERATION_UNIFIED
    if legacy:
        return GENERATION_PRE_MIGRATION
    return GENERATION_UNKNOWN


def _legacy_standins_for_bucket(
    layout: LayoutSpec, bucket: str, present_top_level: List[str]
) -> List[str]:
    """Present root entries whose §4 target lives inside ``bucket``."""
    standins = []
    for name in present_top_level:
        spec = layout.legacy(name)
        if spec is None or not spec.placed:
            continue
        if spec.canonical_target.split("/", 1)[0] == bucket:
            standins.append(name)
    return standins


def _missing_expected(
    root: Path,
    layout: LayoutSpec,
    present_top_level: List[str],
    bucket_children: Dict[str, List[str]],
) -> List[Dict[str, Any]]:
    """Canonical paths §4 requires that are absent, and what stands in for them.

    ``satisfied_by_legacy`` is why a mid-migration host is not a failing host:
    ``ledger/mac.db`` being absent is expected while ``mac.db`` is still at the
    root, and the report says so instead of just calling it missing.
    """
    present = set(present_top_level)
    missing: List[Dict[str, Any]] = []
    for spec in layout.buckets:
        if spec.name not in present:
            missing.append(
                {
                    "relative_path": spec.name,
                    "path": str(root / spec.name),
                    "bucket": spec.name,
                    "kind": "directory",
                    "satisfied_by_legacy": _legacy_standins_for_bucket(
                        layout, spec.name, present_top_level
                    ),
                }
            )
            continue
        children = set(bucket_children.get(spec.name, []))
        for entry in spec.entries:
            if entry in children:
                continue
            target = "%s/%s" % (spec.name, entry)
            missing.append(
                {
                    "relative_path": target,
                    "path": str(root / spec.name / entry),
                    "bucket": spec.name,
                    "kind": "unspecified",
                    "satisfied_by_legacy": [
                        name
                        for name in layout.legacy_sources_for(target)
                        if name in present
                    ],
                }
            )
    return missing


def audit_mac_home(
    root: Optional[Any] = None,
    layout: LayoutSpec = MAC_HOME_LAYOUT,
) -> Dict[str, Any]:
    """Audit the unified MAC home root and return a ``mac.mac_home_audit.v1`` report.

    ``root`` defaults to :func:`mac.mac_paths.mac_home`; pass an explicit path
    to audit an arbitrary tree (this is what makes the module testable without
    touching a real home).  The audit is read-only and never raises for a
    missing, unreadable or non-directory root — that outcome is reported in
    ``root_status``.
    """
    resolved = Path(root).expanduser() if root is not None else mac_paths.mac_home()
    status, error = _root_status(resolved)
    # "missing" is the only status that proves absence: a root we could not
    # stat, or one that is a file, is still something on disk.
    root_exists = status != ROOT_MISSING

    records: List[Dict[str, Any]] = []
    top_level: List[str] = []
    bucket_children: Dict[str, List[str]] = {}
    unreadable: List[Dict[str, str]] = []

    if status == ROOT_OK:
        top_level, error = _list_names(resolved)
        if error is not None:
            status = ROOT_UNREADABLE
            top_level = []

    if status == ROOT_OK:
        for name in top_level:
            classification = classify_root_entry(name, layout)
            record = _entry_record(resolved, name, 0, classification)
            records.append(record)

            bucket = layout.bucket(name)
            if bucket is None or not bucket.enumerated:
                continue
            if record["kind"] != "directory":
                # A bucket that is a file (or a symlink we refuse to follow) has
                # no children to compare; the kind is already in the record.
                continue
            children, child_error = _list_names(resolved / name)
            if child_error is not None:
                unreadable.append({"relative_path": name, "error": child_error})
                continue
            bucket_children[name] = children
            for child in children:
                relative = "%s/%s" % (name, child)
                records.append(
                    _entry_record(
                        resolved, relative, 1, _classify_bucket_child(bucket, child)
                    )
                )

    missing = (
        _missing_expected(resolved, layout, top_level, bucket_children)
        if status == ROOT_OK
        else []
    )

    by_class = {
        CANONICAL: [r["relative_path"] for r in records if r["classification"] == CANONICAL],
        LEGACY_ACCEPTED: [
            r["relative_path"] for r in records if r["classification"] == LEGACY_ACCEPTED
        ],
        DRIFT: [r["relative_path"] for r in records if r["classification"] == DRIFT],
    }
    unplaced = [
        r["relative_path"]
        for r in records
        if r["classification"] == LEGACY_ACCEPTED and r["canonical_target"] is None
    ]
    buckets_present = [
        spec.name for spec in layout.buckets if spec.name in set(top_level)
    ]

    return {
        "schema": MAC_HOME_AUDIT_SCHEMA,
        "root_path": str(resolved),
        "audited_at": _utc_now(),
        "root_exists": root_exists,
        "root_status": status,
        "root_error": error,
        "layout_generation": (
            _overall_generation(records) if status == ROOT_OK else GENERATION_UNKNOWN
        ),
        "entries": records,
        "canonical": by_class[CANONICAL],
        "legacy_accepted": by_class[LEGACY_ACCEPTED],
        "drift": by_class[DRIFT],
        "unplaced_legacy": unplaced,
        "missing_expected": missing,
        "unreadable_paths": unreadable,
        # Reserved for the sibling duplicate-datum / orphan detector.  Present
        # and empty so consumers can rely on the shape before it lands.
        "duplicates": [],
        "orphans": [],
        "summary": {
            "entry_count": len(records),
            "top_level_count": len(top_level),
            "canonical_count": len(by_class[CANONICAL]),
            "legacy_accepted_count": len(by_class[LEGACY_ACCEPTED]),
            "drift_count": len(by_class[DRIFT]),
            "unplaced_legacy_count": len(unplaced),
            "missing_expected_count": len(missing),
            "unreadable_path_count": len(unreadable),
            "duplicate_count": 0,
            "orphan_count": 0,
            "buckets_present": buckets_present,
            "buckets_missing": [
                name for name in layout.bucket_names if name not in buckets_present
            ],
        },
    }
