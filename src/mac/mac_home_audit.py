"""Read-only auditor of the unified ``$MAC_HOME`` root.

The canonical layout from ``docs/home-consolidation.md`` §4 is modelled as a
declarative spec (not ad-hoc conditionals) so detectors and tests consume the
same data. Mid-migration trees are accepted: each observed entry is classified
``canonical``, ``legacy_accepted`` (with its canonical target named), or
``drift``. Duplicate/orphan keys are reserved for a sibling task.

Roots resolve through ``mac.mac_paths`` when not passed explicitly. A missing
or unreadable root is reported in ``status`` and never raised. This module
does not create, write, chmod, or delete anything.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Mapping, Optional, Tuple, Union

from mac import mac_paths
from mac.hermes_home_audit import HERMES_KNOWN_TOP_LEVEL, classify_named_children, safe_iterdir

MAC_HOME_AUDIT_SCHEMA = "mac.mac_home_audit.v1"

CLASS_CANONICAL = "canonical"
CLASS_LEGACY = "legacy_accepted"
CLASS_DRIFT = "drift"

GEN_UNIFIED = "unified"
GEN_PRE_PHASE2 = "pre_phase2"
GEN_MIXED = "mixed"
GEN_UNKNOWN = "unknown"

Pathish = Union[str, Path, None]


@dataclass(frozen=True)
class ExpectedPath:
    """One expected path relative to ``$MAC_HOME``."""

    relative: str
    kind: str  # "file" or "directory"


@dataclass(frozen=True)
class BucketSpec:
    """One top-level bucket of the unified layout and its expected children."""

    name: str
    children: Tuple[str, ...]
    child_kinds: Mapping[str, str]


def _child_kinds(*pairs: Tuple[str, str]) -> Dict[str, str]:
    return dict(pairs)


# docs/home-consolidation.md §4 — the ONE authoritative root.
UNIFIED_LAYOUT: Tuple[BucketSpec, ...] = (
    BucketSpec(
        name="ledger",
        children=("mac.db", "backups", "archive"),
        child_kinds=_child_kinds(
            ("mac.db", "file"),
            ("backups", "directory"),
            ("archive", "directory"),
        ),
    ),
    BucketSpec(
        name="secrets",
        children=("mac.env", ".env", "client-principals.json"),
        child_kinds=_child_kinds(
            ("mac.env", "file"),
            (".env", "file"),
            ("client-principals.json", "file"),
        ),
    ),
    BucketSpec(
        name="fleet",
        children=("fleets.yaml", "specs"),
        child_kinds=_child_kinds(
            ("fleets.yaml", "file"),
            ("specs", "directory"),
        ),
    ),
    BucketSpec(
        name="runtime",
        children=(
            "mac-runtime-context.json",
            "mac-runtime-context.md",
            "mac-memory-topology.json",
            "journal",
        ),
        child_kinds=_child_kinds(
            ("mac-runtime-context.json", "file"),
            ("mac-runtime-context.md", "file"),
            ("mac-memory-topology.json", "file"),
            ("journal", "directory"),
        ),
    ),
    BucketSpec(
        name="gateway",
        children=("openclaw",),
        child_kinds=_child_kinds(("openclaw", "directory")),
    ),
    BucketSpec(
        name="toolchain",
        children=("src", "venv", "bin", "hermes-agent"),
        child_kinds=_child_kinds(
            ("src", "directory"),
            ("venv", "directory"),
            ("bin", "directory"),
            ("hermes-agent", "directory"),
        ),
    ),
)

CANONICAL_BUCKETS: FrozenSet[str] = frozenset(bucket.name for bucket in UNIFIED_LAYOUT)

# Pre-Phase-2 on-disk shape: these names live directly at $MAC_HOME today.
# Each maps to its §4 canonical target.
LEGACY_ROOT_TARGETS: Mapping[str, str] = {
    "mac.db": "ledger/mac.db",
    "backups": "ledger/backups",
    "archive": "ledger/archive",
    "mac.env": "secrets/mac.env",
    ".env": "secrets/.env",
    "client-principals.json": "secrets/client-principals.json",
    "fleets.yaml": "fleet/fleets.yaml",
    "specs": "fleet/specs",
    "mac-runtime-context.json": "runtime/mac-runtime-context.json",
    "mac-runtime-context.md": "runtime/mac-runtime-context.md",
    "mac-memory-topology.json": "runtime/mac-memory-topology.json",
    "journal": "runtime/journal",
    "openclaw": "gateway/openclaw",
    "src": "toolchain/src",
    "venv": "toolchain/venv",
    "bin": "toolchain/bin",
    "hermes-agent": "toolchain/hermes-agent",
    # Recognised current $MAC_HOME owners not yet drawn in the §4 tree graphic.
    "qdrant": "runtime/qdrant",
    "openshell": "runtime/openshell",
    "openshell-policy.yaml": "runtime/openshell-policy.yaml",
    "public-artifacts": "runtime/public-artifacts",
    "sessions": "runtime/sessions",
    "ssh": "secrets/ssh",
    "clients": "fleet/clients",
    "credentials": "secrets/credentials",
    "agent-footprint.json": "toolchain/agent-footprint.json",
}

GATEWAY_CHILD_KNOWN: FrozenSet[str] = frozenset(set(HERMES_KNOWN_TOP_LEVEL) | {"openclaw"})

_BUCKET_BY_NAME: Dict[str, BucketSpec] = {bucket.name: bucket for bucket in UNIFIED_LAYOUT}


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _kind(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return "inaccessible"
    if stat.S_ISLNK(mode):
        return "symlink"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "other"


def _status_for_root(root: Path) -> str:
    if not _exists(root):
        return "missing"
    try:
        is_dir = root.is_dir()
    except OSError:
        return "unreadable"
    if not is_dir:
        return "not_a_directory"
    try:
        os.listdir(root)
    except OSError:
        return "unreadable"
    return "ok"


def _resolve_root(root: Pathish, home: Pathish) -> Path:
    chosen = root if root is not None else home
    if chosen is None:
        return mac_paths.mac_home()
    return Path(chosen)


def expected_paths() -> Tuple[ExpectedPath, ...]:
    """Flatten UNIFIED_LAYOUT into the expected canonical relative paths."""
    items: List[ExpectedPath] = []
    for bucket in UNIFIED_LAYOUT:
        items.append(ExpectedPath(relative=bucket.name, kind="directory"))
        for child in bucket.children:
            items.append(
                ExpectedPath(
                    relative="%s/%s" % (bucket.name, child),
                    kind=bucket.child_kinds.get(child, "file"),
                )
            )
    return tuple(items)


def _legacy_satisfier(canonical_relative: str, observed_names: FrozenSet[str]) -> Optional[str]:
    for legacy_name, target in LEGACY_ROOT_TARGETS.items():
        if target == canonical_relative and legacy_name in observed_names:
            return legacy_name
    return None


def _classify_root_name(name: str) -> Dict[str, Any]:
    if name in CANONICAL_BUCKETS:
        return {
            "classification": CLASS_CANONICAL,
            "generation": GEN_UNIFIED,
            "canonical_target": name,
        }
    target = LEGACY_ROOT_TARGETS.get(name)
    if target is not None:
        return {
            "classification": CLASS_LEGACY,
            "generation": GEN_PRE_PHASE2,
            "canonical_target": target,
        }
    return {
        "classification": CLASS_DRIFT,
        "generation": GEN_UNKNOWN,
        "canonical_target": None,
    }


def _bucket_known_names(bucket: BucketSpec) -> FrozenSet[str]:
    if bucket.name == "gateway":
        return GATEWAY_CHILD_KNOWN
    extra = set(LEGACY_ROOT_TARGETS.values())
    # Allow known extra children whose canonical target is inside this bucket.
    extras = {Path(target).name for target in extra if Path(target).parts[0] == bucket.name}
    return frozenset(set(bucket.children) | extras)


def _generation_detected(classifications: Iterable[str]) -> str:
    kinds = frozenset(classifications)
    has_canonical = CLASS_CANONICAL in kinds
    has_legacy = CLASS_LEGACY in kinds
    if has_canonical and has_legacy:
        return GEN_MIXED
    if has_canonical:
        return GEN_UNIFIED
    if has_legacy:
        return GEN_PRE_PHASE2
    return GEN_UNKNOWN


def _entry(
    *,
    rel: str,
    name: str,
    kind: str,
    classification: str,
    generation: str,
    canonical_target: Optional[str],
    container: str,
) -> Dict[str, Any]:
    return {
        "path": rel,
        "name": name,
        "kind": kind,
        "classification": classification,
        "generation": generation,
        "canonical_target": canonical_target,
        "container": container,
    }


def audit_mac_home(root: Pathish = None, *, home: Pathish = None) -> Dict[str, Any]:
    """Read-only audit of ``$MAC_HOME``.

    Pass ``root`` or ``home`` to audit an explicit tree (tests). When both are
    omitted the root is ``mac_paths.mac_home()``.
    """
    resolved = _resolve_root(root, home)
    status = _status_for_root(resolved)
    root_exists = status != "missing"
    entries: List[Dict[str, Any]] = []
    observed_names: FrozenSet[str] = frozenset()

    if status == "ok":
        top_children = safe_iterdir(resolved)
        observed_names = frozenset(child.name for child in top_children)
        for child in top_children:
            meta = _classify_root_name(child.name)
            entries.append(
                _entry(
                    rel=child.name,
                    name=child.name,
                    kind=_kind(child),
                    classification=meta["classification"],
                    generation=meta["generation"],
                    canonical_target=meta["canonical_target"],
                    container="",
                )
            )
            if child.name in CANONICAL_BUCKETS and _kind(child) in {"directory", "symlink"}:
                bucket = _BUCKET_BY_NAME[child.name]
                known = _bucket_known_names(bucket)
                nested = classify_named_children(child, known, container=child.name)
                for item in nested:
                    if item["classification"] == CLASS_CANONICAL:
                        generation = GEN_UNIFIED
                        target = item["path"]
                    else:
                        generation = GEN_UNKNOWN
                        target = None
                    entries.append(
                        _entry(
                            rel=item["path"],
                            name=item["name"],
                            kind=item["kind"],
                            classification=item["classification"],
                            generation=generation,
                            canonical_target=target,
                            container=item["container"],
                        )
                    )

    missing_expected: List[Dict[str, Any]] = []
    if status == "ok":
        for expected in expected_paths():
            path = resolved / expected.relative
            if _exists(path):
                continue
            missing_expected.append(
                {
                    "path": expected.relative,
                    "kind": expected.kind,
                    "satisfied_by_legacy": _legacy_satisfier(expected.relative, observed_names),
                }
            )
    elif status == "missing":
        for expected in expected_paths():
            missing_expected.append(
                {
                    "path": expected.relative,
                    "kind": expected.kind,
                    "satisfied_by_legacy": None,
                }
            )

    summary_classifications = [
        item["classification"] for item in entries if item["container"] == ""
    ]
    duplicates: List[Any] = []
    orphans: List[Any] = []
    canonical_count = sum(1 for item in entries if item["classification"] == CLASS_CANONICAL)
    legacy_count = sum(1 for item in entries if item["classification"] == CLASS_LEGACY)
    drift_count = sum(1 for item in entries if item["classification"] == CLASS_DRIFT)

    return {
        "schema": MAC_HOME_AUDIT_SCHEMA,
        "root_path": str(resolved),
        "audited_at": _utc_now(),
        "root_exists": root_exists,
        "status": status,
        "layout": {
            "generation_detected": _generation_detected(summary_classifications),
            "buckets": [bucket.name for bucket in UNIFIED_LAYOUT],
        },
        "entries": entries,
        "missing_expected": missing_expected,
        "duplicates": duplicates,
        "orphans": orphans,
        "summary": {
            "entry_count": len(entries),
            "canonical": canonical_count,
            "legacy_accepted": legacy_count,
            "drift": drift_count,
            "missing_expected": len(missing_expected),
            "duplicates": 0,
            "orphans": 0,
        },
    }


__all__ = [
    "MAC_HOME_AUDIT_SCHEMA",
    "UNIFIED_LAYOUT",
    "LEGACY_ROOT_TARGETS",
    "CANONICAL_BUCKETS",
    "audit_mac_home",
    "expected_paths",
]
