"""Strictly read-only auditor for the unified ``$MAC_HOME`` root.

``docs/home-consolidation.md`` §4 approves a single authoritative root with six
top-level buckets::

    $MAC_HOME
    ├── ledger/     mac.db, backups, archive
    ├── secrets/    mac.env, .env, client-principals.json
    ├── fleet/      fleets.yaml, specs
    ├── runtime/    mac-runtime-context.*, mac-memory-topology.json, journal/
    ├── gateway/    (today's gateway home) └── openclaw/
    └── toolchain/  src, venv, bin, hermes-agent

That target shape is modelled here as **data** (:data:`CANONICAL_LAYOUT`) rather
than as a pile of conditionals, so the drift detector, the duplicate/orphan
detectors landing alongside it, and the tests all consume one description of the
layout instead of three drifting copies.

The fleet is mid-migration, so the auditor is deliberately bi-lingual. The
pre-Phase-2 shape puts the very same data *directly at the root*
(``mac.db``, ``mac.env``, ``fleets.yaml``, ``journal/``, ``backups/``,
``openclaw/``, ...). Those locations are recognised as ``legacy_accepted`` and
reported **with the canonical target named**, so a legacy host produces a
migration map, not a wall of false drift. Only genuinely unknown entries are
``drift``. The acceptance is derived from the canonical spec itself
(:attr:`BucketSpec.legacy_at_root`), which is why the two can never disagree.

Two hard guarantees, both covered by tests:

  * **Read-only.** Nothing here creates, writes, chmods, or deletes. The audit
    only lists directories and stats entries without following symlinks.
  * **Never raises on a hostile tree.** A missing, unreadable, or not-a-directory
    root is reported in ``status``; it is not an exception.

Every root is resolved through :mod:`mac.mac_paths` — the one module allowed to
name the home literals (``tests/test_mac_paths_no_hardcode.py`` enforces it).

Historical note: ``src/mac/hermes_home_audit.py`` was the single-home ancestor of
this module. It was deleted as dead code (never wired to a caller) before this
landed, so there is no allow-list left to import; the canonical gateway entries
below are re-derived from ``docs/home-consolidation.md`` §1.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from mac import mac_paths
from mac.models import utcnow

MAC_HOME_AUDIT_SCHEMA = "mac.mac_home_audit.v1"

#: An entry sitting in the approved §4 location.
CANONICAL = "canonical"
#: An entry in its recognised pre-migration location (canonical target named).
LEGACY_ACCEPTED = "legacy_accepted"
#: An entry the canonical spec does not describe at all.
DRIFT = "drift"

#: Layout generation an observed entry belongs to.
GENERATION_UNIFIED = "unified"
GENERATION_PRE_MIGRATION = "pre_migration"
GENERATION_UNKNOWN = "unknown"

STATUS_OK = "ok"
STATUS_MISSING_ROOT = "missing_root"
STATUS_NOT_A_DIRECTORY = "root_not_a_directory"
STATUS_UNREADABLE_ROOT = "unreadable_root"


@dataclass(frozen=True)
class BucketSpec:
    """One canonical top-level bucket of the unified layout.

    ``entries`` are the paths §4 enumerates for the bucket. ``legacy_at_root``
    is the subset that is *also* accepted directly at ``$MAC_HOME`` today,
    because that is where the pre-migration tree keeps them; each one's
    canonical target is then ``<bucket>/<entry>``. The gateway bucket lists only
    ``openclaw`` because the rest of its content lives in a *different* legacy
    root (the gateway home), never at the MAC root.
    """

    name: str
    purpose: str
    entries: Tuple[str, ...]
    legacy_at_root: Tuple[str, ...] = ()

    def accepts_at_root(self, name: str) -> bool:
        """True when ``name`` at the root is this bucket's pre-migration home."""
        return name in self.legacy_at_root

    def canonical_target(self, name: str) -> str:
        """Posix path, relative to the root, where ``name`` belongs after Phase 2."""
        return f"{self.name}/{name}"


_LEDGER = BucketSpec(
    name="ledger",
    purpose="hub SQLite ledger plus its backups and archive",
    entries=("mac.db", "backups", "archive"),
    legacy_at_root=("mac.db", "backups", "archive"),
)
_SECRETS = BucketSpec(
    name="secrets",
    purpose="the single secret source (hub, client, principals)",
    entries=("mac.env", ".env", "client-principals.json"),
    legacy_at_root=("mac.env", ".env", "client-principals.json"),
)
_FLEET = BucketSpec(
    name="fleet",
    purpose="fleet registry and specs",
    entries=("fleets.yaml", "specs"),
    legacy_at_root=("fleets.yaml", "specs"),
)
_RUNTIME = BucketSpec(
    name="runtime",
    purpose="control-plane runtime artefacts MAC writes about itself",
    entries=(
        "mac-runtime-context.json",
        "mac-runtime-context.md",
        "mac-memory-topology.json",
        "journal",
    ),
    legacy_at_root=(
        "mac-runtime-context.json",
        "mac-runtime-context.md",
        "mac-memory-topology.json",
        "journal",
    ),
)
_GATEWAY = BucketSpec(
    name="gateway",
    purpose="agent-personal gateway home (0700), with OpenClaw nested under it",
    # Re-derived from docs/home-consolidation.md §1 ("Owns (authoritative)").
    entries=(
        "openclaw",
        "SOUL.md",
        "USER.md",
        "MEMORY.md",
        "memories",
        "mood",
        "config.yaml",
        ".env",
        "auth.json",
        "state.db",
        "sessions",
        "skills",
        "plugins",
        "cron",
        "logs",
        "dream_logs",
        "scripts",
    ),
    # Only OpenClaw is at the MAC root pre-migration (.mac/openclaw); the rest
    # of the gateway's content lives in the gateway home, a separate tree.
    legacy_at_root=("openclaw",),
)
_TOOLCHAIN = BucketSpec(
    name="toolchain",
    purpose="installed source, venv, entry points and the agent runtime",
    entries=("src", "venv", "bin", "hermes-agent"),
    legacy_at_root=("src", "venv", "bin", "hermes-agent"),
)

#: The canonical unified layout, keyed by bucket name. The declarative spec the
#: detectors and the tests both consume.
CANONICAL_LAYOUT: Dict[str, BucketSpec] = {
    spec.name: spec
    for spec in (_LEDGER, _SECRETS, _FLEET, _RUNTIME, _GATEWAY, _TOOLCHAIN)
}

#: Root-level name -> canonical target, derived from the spec so the accepted
#: pre-migration locations can never drift from the layout they migrate into.
LEGACY_ROOT_TARGETS: Dict[str, str] = {
    entry: spec.canonical_target(entry)
    for spec in CANONICAL_LAYOUT.values()
    for entry in spec.legacy_at_root
}


def canonical_bucket_names() -> Tuple[str, ...]:
    """The approved top-level bucket names, in layout order."""
    return tuple(CANONICAL_LAYOUT)


def classify_root_name(name: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Classify one top-level name.

    Returns ``(classification, bucket_name, canonical_target)``. ``bucket_name``
    is the owning bucket when known; ``canonical_target`` is set only for a
    ``legacy_accepted`` entry, naming where Phase 2 moves it.
    """
    if name in CANONICAL_LAYOUT:
        return CANONICAL, name, None
    for spec in CANONICAL_LAYOUT.values():
        if spec.accepts_at_root(name):
            return LEGACY_ACCEPTED, spec.name, spec.canonical_target(name)
    return DRIFT, None, None


_GENERATION_BY_CLASSIFICATION = {
    CANONICAL: GENERATION_UNIFIED,
    LEGACY_ACCEPTED: GENERATION_PRE_MIGRATION,
    DRIFT: GENERATION_UNKNOWN,
}


def _entry_kind(path: Path) -> str:
    """Describe an entry without ever following a symlink (or raising)."""
    try:
        if path.is_symlink():
            return "symlink"
        if path.is_dir():
            return "dir"
        if path.is_file():
            return "file"
    except OSError:  # pragma: no cover - defensive; the checks swallow most
        return "unknown"
    return "other"


def _list_dir(path: Path) -> Tuple[List[str], Optional[str]]:
    """Return ``(sorted names, error)``. Listing only — nothing is created."""
    try:
        return sorted(os.listdir(path)), None
    except OSError as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _make_entry(
    path: Path,
    relative: str,
    depth: int,
    classification: str,
    bucket: Optional[str],
    canonical_target: Optional[str],
) -> Dict[str, Any]:
    return {
        "name": path.name,
        "relative_path": relative,
        "path": str(path),
        "depth": depth,
        "kind": _entry_kind(path),
        "classification": classification,
        "generation": _GENERATION_BY_CLASSIFICATION[classification],
        "bucket": bucket,
        "canonical_target": canonical_target,
    }


def _audit_bucket_children(root: Path, spec: BucketSpec) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """One level deeper: flag non-standard entries inside a canonical bucket."""
    bucket_dir = root / spec.name
    if not _is_dir(bucket_dir):
        return [], None
    names, error = _list_dir(bucket_dir)
    entries = [
        _make_entry(
            bucket_dir / name,
            f"{spec.name}/{name}",
            2,
            CANONICAL if name in spec.entries else DRIFT,
            spec.name,
            None,
        )
        for name in names
    ]
    return entries, error


def _is_dir(path: Path) -> bool:
    """True for a real directory, symlinks not followed, errors swallowed."""
    try:
        return os.path.isdir(path) and not path.is_symlink()
    except OSError:  # pragma: no cover - defensive, isdir already swallows
        return False


def _exists(path: Path) -> bool:
    """Existence check that also sees a broken symlink, and never raises."""
    try:
        return path.exists() or path.is_symlink()
    except OSError:  # pragma: no cover - defensive
        return False


def _expectations(root: Path) -> List[Dict[str, Any]]:
    """Where each enumerated canonical path actually is, if anywhere."""
    results: List[Dict[str, Any]] = []
    for spec in CANONICAL_LAYOUT.values():
        for entry in spec.entries:
            canonical_path = f"{spec.name}/{entry}"
            legacy_path = entry if spec.accepts_at_root(entry) else None
            if _exists(root / spec.name / entry):
                status = CANONICAL
            elif legacy_path is not None and _exists(root / legacy_path):
                status = LEGACY_ACCEPTED
            else:
                status = "missing"
            results.append(
                {
                    "bucket": spec.name,
                    "entry": entry,
                    "canonical_path": canonical_path,
                    "legacy_path": legacy_path,
                    "status": status,
                }
            )
    return results


def _layout_generation(entries: List[Dict[str, Any]]) -> str:
    """Which generation the observed root belongs to, overall."""
    generations = {
        entry["generation"] for entry in entries if entry["depth"] == 1
    }
    unified = GENERATION_UNIFIED in generations
    pre = GENERATION_PRE_MIGRATION in generations
    if unified and pre:
        return "mixed"
    if unified:
        return GENERATION_UNIFIED
    if pre:
        return GENERATION_PRE_MIGRATION
    return GENERATION_UNKNOWN


def _empty_report(root: Path, status: str, detail: Optional[str], root_exists: bool) -> Dict[str, Any]:
    """A well-formed report for a root that could not be walked.

    ``_expectations`` still runs: under an absent root every probe simply misses,
    which is exactly the right answer, and it keeps the report shape identical
    to a successful audit so consumers need no special case.
    """
    return _report(
        root=root,
        status=status,
        status_detail=detail,
        root_exists=root_exists,
        entries=[],
        expectations=_expectations(root),
        read_errors=[],
    )


def _report(
    *,
    root: Path,
    status: str,
    status_detail: Optional[str],
    root_exists: bool,
    entries: List[Dict[str, Any]],
    expectations: List[Dict[str, Any]],
    read_errors: List[Dict[str, str]],
) -> Dict[str, Any]:
    by_classification: Dict[str, List[str]] = {CANONICAL: [], LEGACY_ACCEPTED: [], DRIFT: []}
    for entry in entries:
        by_classification[entry["classification"]].append(entry["relative_path"])
    missing_expected = [
        item["canonical_path"] for item in expectations if item["status"] == "missing"
    ]
    # Reserved for the sibling duplicate-datum / orphan detectors. Present and
    # empty from v1 so consumers can rely on the shape before they land.
    duplicates: List[Dict[str, Any]] = []
    orphans: List[Dict[str, Any]] = []
    return {
        "schema": MAC_HOME_AUDIT_SCHEMA,
        "root_path": str(root),
        "audited_at": utcnow(),
        "root_exists": root_exists,
        "status": status,
        "status_detail": status_detail,
        "layout_generation": _layout_generation(entries),
        "entries": entries,
        "by_classification": by_classification,
        "expectations": expectations,
        "missing_expected": missing_expected,
        "read_errors": read_errors,
        "duplicates": duplicates,
        "orphans": orphans,
        "summary": {
            "entry_count": len(entries),
            "top_level_count": sum(1 for entry in entries if entry["depth"] == 1),
            "canonical_count": len(by_classification[CANONICAL]),
            "legacy_accepted_count": len(by_classification[LEGACY_ACCEPTED]),
            "drift_count": len(by_classification[DRIFT]),
            "missing_expected_count": len(missing_expected),
            "read_error_count": len(read_errors),
            "duplicate_count": len(duplicates),
            "orphan_count": len(orphans),
        },
    }


def audit_mac_home(root: Optional[Path | str] = None) -> Dict[str, Any]:
    """Audit the unified MAC home and return a ``mac.mac_home_audit.v1`` report.

    ``root`` defaults to :func:`mac.mac_paths.mac_home`; pass an explicit path
    for testing or to audit a staged tree. The audit is read-only and total: a
    missing or unreadable root is reported in ``status``, never raised.
    """
    root = Path(root) if root is not None else mac_paths.mac_home()
    if not _exists(root):
        return _empty_report(root, STATUS_MISSING_ROOT, None, root_exists=False)
    if not _is_dir(root):
        return _empty_report(root, STATUS_NOT_A_DIRECTORY, None, root_exists=True)

    names, error = _list_dir(root)
    if error is not None:
        return _empty_report(root, STATUS_UNREADABLE_ROOT, error, root_exists=True)

    entries: List[Dict[str, Any]] = []
    read_errors: List[Dict[str, str]] = []
    for name in names:
        classification, bucket, target = classify_root_name(name)
        entries.append(_make_entry(root / name, name, 1, classification, bucket, target))
    for spec in CANONICAL_LAYOUT.values():
        children, child_error = _audit_bucket_children(root, spec)
        entries.extend(children)
        if child_error is not None:
            read_errors.append({"path": spec.name, "error": child_error})

    entries.sort(key=lambda entry: (entry["depth"], entry["relative_path"]))
    return _report(
        root=root,
        status=STATUS_OK,
        status_detail=None,
        root_exists=True,
        entries=entries,
        expectations=_expectations(root),
        read_errors=read_errors,
    )


def gateway_layout_position() -> Dict[str, Any]:
    """Where the gateway / OpenClaw homes resolve, relative to the MAC root.

    Reported separately because ``gateway_home()`` is relocatable out of the
    root entirely (an un-migrated host still pins it elsewhere), in which case
    the ``gateway/`` bucket audit above cannot see it. Resolution goes through
    :mod:`mac.mac_paths` only.
    """
    root = mac_paths.mac_home()
    gateway = mac_paths.gateway_home()
    openclaw = mac_paths.openclaw_home()

    def _position(path: Path) -> Dict[str, Any]:
        try:
            relative: Optional[str] = path.relative_to(root).as_posix()
        except ValueError:
            relative = None
        return {
            "path": str(path),
            "exists": _exists(path),
            "inside_root": relative is not None,
            "relative_path": relative,
        }

    return {
        "root_path": str(root),
        "gateway": _position(gateway),
        "openclaw": _position(openclaw),
    }
