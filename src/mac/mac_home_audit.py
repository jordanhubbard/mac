"""Strictly read-only auditor of the unified ``$MAC_HOME`` root.

``docs/home-consolidation.md`` §4 approves a single authoritative root with six
top-level buckets (``ledger/``, ``secrets/``, ``fleet/``, ``runtime/``,
``gateway/`` — which contains ``openclaw/`` — and ``toolchain/``).  The fleet is
mid-migration: today's on-disk shape is still the pre-Phase-2 one, with
``mac.db``, ``mac.env``, ``fleets.yaml``, ``openclaw/``, ``journal/`` and
``backups/`` sitting directly at the root.  An auditor that only knew the target
layout would report the entire live fleet as broken, so this module models
*both* generations as data and classifies every observed entry as one of:

``canonical``
    The entry sits where the §4 target layout puts it.
``legacy_accepted``
    A recognised pre-migration location.  ``canonical_target`` names where §4
    moves it (``null`` for the recognised entries §4 does not place — see
    :data:`LEGACY_ROOT_ENTRIES`).
``drift``
    Not named by either generation: unknown metadata, the thing this audit
    exists to surface.

Design notes
------------
* **The layout is data, not conditionals.**  :data:`CANONICAL_BUCKETS`,
  :data:`LEGACY_ROOT_ENTRIES` and :data:`GATEWAY_HOME_ENTRIES` are declarative
  specs; the detectors and the tests both consume them, and the
  duplicate/orphan detectors added by the sibling task consume them too (this
  report already reserves stable ``duplicates``/``orphans`` keys so that
  addition does not change the schema).
* **Strictly read-only.**  Nothing here creates, writes, chmods or deletes.
  The only filesystem calls are ``iterdir``/``exists``/``is_dir``/``is_file``/
  ``lstat``/``readlink``.  ``tests/test_mac_home_audit.py`` snapshots the
  fixture tree's listing and mtimes around an audit and asserts they are
  unchanged.
* **Never raises.**  A missing, unreadable or non-directory root is reported in
  ``status``; a directory that cannot be listed is reported in
  ``unreadable_paths``.  Auditing a legacy tree is a normal, quiet outcome.
* **Roots resolve only through :mod:`mac.mac_paths`.**  No home-directory
  literal is joined onto ``Path.home()`` anywhere in this module — that is the
  one thing ``mac_paths`` is allowed to do, and
  ``tests/test_mac_paths_no_hardcode.py`` enforces it.  The explicit path
  arguments exist purely for testability.

The gateway allow-list is exported as :data:`GATEWAY_HOME_ENTRIES` rather than
kept private so that a gateway-home auditor reuses it instead of copying it.
The earlier ``src/mac/hermes_home_audit.py`` that this module generalizes was
deleted as uncalled dead code before this change (see
``docs/home-consolidation.md`` §5 "Cross-cutting"), so there is no in-tree
caller left to refactor onto it.
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
    "CLASSIFICATION_CANONICAL",
    "CLASSIFICATION_LEGACY",
    "CLASSIFICATION_DRIFT",
    "LAYOUT_UNIFIED",
    "LAYOUT_PRE_PHASE_2",
    "LAYOUT_UNKNOWN",
    "STATUS_OK",
    "STATUS_MISSING_ROOT",
    "STATUS_NOT_A_DIRECTORY",
    "STATUS_UNREADABLE_ROOT",
    "ExpectedEntry",
    "Bucket",
    "LegacyEntry",
    "CANONICAL_BUCKETS",
    "LEGACY_ROOT_ENTRIES",
    "GATEWAY_HOME_ENTRIES",
    "canonical_bucket_names",
    "expected_paths",
    "legacy_target_for",
    "container_allow_list",
    "audit_mac_home",
]

MAC_HOME_AUDIT_SCHEMA = "mac.mac_home_audit.v1"

# --- Classification vocabulary ---------------------------------------------

CLASSIFICATION_CANONICAL = "canonical"
CLASSIFICATION_LEGACY = "legacy_accepted"
CLASSIFICATION_DRIFT = "drift"

#: Layout generation an entry belongs to.
LAYOUT_UNIFIED = "unified"  # the §4 target shape
LAYOUT_PRE_PHASE_2 = "pre_phase_2"  # today's on-disk shape
LAYOUT_UNKNOWN = "unknown"  # neither generation names it

STATUS_OK = "ok"
STATUS_MISSING_ROOT = "missing_root"
STATUS_NOT_A_DIRECTORY = "not_a_directory"
STATUS_UNREADABLE_ROOT = "unreadable_root"


# --- Declarative layout spec ------------------------------------------------


@dataclass(frozen=True)
class ExpectedEntry:
    """One enumerated member of a canonical bucket.

    ``descend`` marks a container whose *contents* are themselves auditable
    against an allow-list (only the gateway home today).  Dated or unbounded
    directories — ``backups``, ``archive``, ``journal``, ``specs``, ``venv`` —
    are deliberately opaque: their contents are data, not layout, and walking
    them would produce drift reports for every dated snapshot.
    """

    name: str
    kind: str  # "dir" | "file"
    required: bool = False
    descend: bool = False
    purpose: str = ""


@dataclass(frozen=True)
class Bucket:
    """A top-level bucket of the §4 unified layout."""

    name: str
    purpose: str
    entries: Tuple[ExpectedEntry, ...]

    @property
    def entry_names(self) -> Tuple[str, ...]:
        return tuple(entry.name for entry in self.entries)


@dataclass(frozen=True)
class LegacyEntry:
    """A recognised pre-Phase-2 entry sitting directly at the root.

    ``canonical_target`` is the §4 destination as a ``bucket/name`` string, or
    ``None`` for an entry the target layout does not (yet) place — recognising
    it as pre-existing fleet state is still strictly better than calling it
    drift.
    """

    name: str
    kind: str  # "dir" | "file"
    canonical_target: Optional[str] = None
    note: str = ""


#: The §4 target layout.  ``docs/home-consolidation.md`` is the source; each
#: bucket's entries are exactly the ones enumerated there.
CANONICAL_BUCKETS: Tuple[Bucket, ...] = (
    Bucket(
        name="ledger",
        purpose="Hub ledger database plus its backup and archive trees.",
        entries=(
            ExpectedEntry("mac.db", "file", required=True, purpose="Hub SQLite ledger"),
            ExpectedEntry("backups", "dir", purpose="Dated ledger/deploy rollback backups"),
            ExpectedEntry("archive", "dir", purpose="Ledger archive"),
        ),
    ),
    Bucket(
        name="secrets",
        purpose="The single secret source; 0700 dir, 0600 files.",
        entries=(
            ExpectedEntry("mac.env", "file", required=True, purpose="Hub/service secrets"),
            ExpectedEntry(".env", "file", purpose="Client deploy env (scoped fleet tokens)"),
            ExpectedEntry(
                "client-principals.json", "file", purpose="Client principal registry"
            ),
        ),
    ),
    Bucket(
        name="fleet",
        purpose="Fleet registry and fleet specs.",
        entries=(
            ExpectedEntry("fleets.yaml", "file", required=True, purpose="Fleet registry"),
            ExpectedEntry("specs", "dir", purpose="Fleet spec documents"),
        ),
    ),
    Bucket(
        name="runtime",
        purpose="Control-plane runtime artefacts (Phase 1 moves these out of the gateway home).",
        entries=(
            ExpectedEntry("mac-runtime-context.json", "file", purpose="Runtime context (machine)"),
            ExpectedEntry("mac-runtime-context.md", "file", purpose="Runtime context (human)"),
            ExpectedEntry("mac-memory-topology.json", "file", purpose="Memory topology"),
            ExpectedEntry("journal", "dir", purpose="Dated soul/memory snapshot backups"),
        ),
    ),
    Bucket(
        name="gateway",
        purpose="The agent-personal gateway home (Phase 2 destination of the legacy tree).",
        entries=(
            ExpectedEntry(
                "openclaw",
                "dir",
                required=True,
                descend=True,
                purpose="OpenClaw runtime home (already under the root today)",
            ),
        ),
    ),
    Bucket(
        name="toolchain",
        purpose="Installed source, virtualenv and executables.",
        entries=(
            ExpectedEntry("src", "dir", purpose="Installed MAC source"),
            ExpectedEntry("venv", "dir", purpose="Installed virtualenv"),
            ExpectedEntry("bin", "dir", purpose="Installed executables"),
            ExpectedEntry("hermes-agent", "dir", purpose="Legacy gateway agent checkout"),
        ),
    ),
)


#: Entries accepted directly at the root because that is where they live today.
#:
#: The names are grounded in live call sites (``mac_paths`` resolvers and the
#: ``mac_home() / "..."`` joins across ``src/mac``), not guessed, so an audit of
#: a real pre-migration host reports zero drift.  Entries with
#: ``canonical_target=None`` are recognised current state that §4 does not
#: place; assigning them a bucket is a layout decision this read-only auditor
#: does not get to make.
LEGACY_ROOT_ENTRIES: Tuple[LegacyEntry, ...] = (
    LegacyEntry("mac.db", "file", "ledger/mac.db"),
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
    LegacyEntry("src", "dir", "toolchain/src"),
    LegacyEntry("venv", "dir", "toolchain/venv"),
    LegacyEntry("bin", "dir", "toolchain/bin"),
    LegacyEntry("hermes-agent", "dir", "toolchain/hermes-agent"),
    # Recognised current state with no §4 destination.
    LegacyEntry("qdrant", "dir", None, "L2 memory store (docs/home-consolidation.md §1)"),
    LegacyEntry("openshell", "dir", None, "OpenShell sandbox state"),
    LegacyEntry("openshell-policy.yaml", "file", None, "Deployed OpenShell policy"),
    LegacyEntry("public-artifacts", "dir", None, "Published artifact drop"),
    LegacyEntry("clients", "dir", None, "Per-client state"),
    LegacyEntry("credentials", "dir", None, "Host credential material"),
    LegacyEntry("sessions", "dir", None, "Session state"),
    LegacyEntry("ssh", "dir", None, "Fleet SSH material"),
    LegacyEntry("agent-footprint.json", "file", None, "Agent footprint report"),
    LegacyEntry("fleet-release-publication.lock", "file", None, "Release publication lock"),
    LegacyEntry(".install.lock", "file", None, "Installer lock"),
)


#: Allow-list for the gateway home's own top level, generalized from the
#: 66-entry list the deleted ``hermes_home_audit`` carried for ``~/.hermes``.
#: It is public so a gateway-home auditor reuses this list instead of copying
#: it.  ``gateway_home()`` and ``openclaw_home()`` resolve to the same tree
#: today, and this list is applied at both the canonical (``gateway/openclaw``)
#: and legacy (``openclaw``) positions.
GATEWAY_HOME_ENTRIES: Tuple[ExpectedEntry, ...] = (
    ExpectedEntry("SOUL.md", "file", purpose="Agent identity"),
    ExpectedEntry("USER.md", "file", purpose="Operator profile"),
    ExpectedEntry("MEMORY.md", "file", purpose="Memory index"),
    ExpectedEntry("memories", "dir", purpose="Memory files"),
    ExpectedEntry("workspace", "dir", purpose="Gateway workspace"),
    ExpectedEntry("config.yaml", "file", purpose="Gateway config (the only name it reads)"),
    ExpectedEntry(".env", "file", purpose="Gateway secrets"),
    ExpectedEntry("auth.json", "file", purpose="Gateway credentials"),
    ExpectedEntry("state.db", "file", purpose="Gateway session database"),
    ExpectedEntry("sessions", "dir", purpose="Session transcripts"),
    ExpectedEntry("skills", "dir", purpose="Installed skills"),
    ExpectedEntry("plugins", "dir", purpose="Installed plugins"),
    ExpectedEntry("cron", "dir", purpose="Gateway cron definitions"),
    ExpectedEntry("logs", "dir", purpose="Gateway logs"),
    ExpectedEntry("dream_logs", "dir", purpose="Dream-cycle reports (imported by dream_log_import)"),
    ExpectedEntry("mood", "dir", purpose="Mood state"),
    ExpectedEntry("script-jobs", "dir", purpose="Host script-job home (§5c)"),
    ExpectedEntry("host-script-jobs.json", "file", purpose="Host script-job definitions"),
    ExpectedEntry("scripts", "dir", purpose="Pre-untangle script home (read-only fallback)"),
    ExpectedEntry("slack_home_channels.json", "file", purpose="Slack home channel map"),
)


# --- Spec accessors ---------------------------------------------------------


def canonical_bucket_names() -> Tuple[str, ...]:
    """Names of the §4 top-level buckets, in document order."""
    return tuple(bucket.name for bucket in CANONICAL_BUCKETS)


def expected_paths() -> Tuple[str, ...]:
    """Every canonical path the spec enumerates, as ``bucket`` / ``bucket/name``."""
    paths: List[str] = []
    for bucket in CANONICAL_BUCKETS:
        paths.append(bucket.name)
        paths.extend(f"{bucket.name}/{entry.name}" for entry in bucket.entries)
    return tuple(paths)


def legacy_target_for(name: str) -> Optional[str]:
    """Canonical ``bucket/name`` destination for a legacy root entry, if any."""
    entry = _LEGACY_BY_NAME.get(name)
    return entry.canonical_target if entry else None


def container_allow_list(relative_path: str) -> Optional[Tuple[ExpectedEntry, ...]]:
    """Allowed entries one level inside a well-known container, or ``None``.

    ``relative_path`` is relative to the audited root, so both the canonical
    (``gateway/openclaw``) and the legacy (``openclaw``) positions of the
    gateway home resolve to :data:`GATEWAY_HOME_ENTRIES`.
    """
    return _CONTAINERS.get(relative_path)


_BUCKET_BY_NAME: Dict[str, Bucket] = {bucket.name: bucket for bucket in CANONICAL_BUCKETS}
_LEGACY_BY_NAME: Dict[str, LegacyEntry] = {entry.name: entry for entry in LEGACY_ROOT_ENTRIES}


def _build_containers() -> Dict[str, Tuple[ExpectedEntry, ...]]:
    """Containers whose immediate children are themselves auditable.

    Buckets are audited against their own enumerated entries; a ``descend``
    entry (the gateway home) is audited against the shared allow-list, and any
    legacy root entry whose canonical target is such a container inherits the
    same list at its pre-migration position.
    """
    containers: Dict[str, Tuple[ExpectedEntry, ...]] = {
        bucket.name: bucket.entries for bucket in CANONICAL_BUCKETS
    }
    for bucket in CANONICAL_BUCKETS:
        for entry in bucket.entries:
            if entry.descend:
                containers[f"{bucket.name}/{entry.name}"] = GATEWAY_HOME_ENTRIES
    for legacy in LEGACY_ROOT_ENTRIES:
        if legacy.canonical_target and legacy.canonical_target in containers:
            containers[legacy.name] = containers[legacy.canonical_target]
    return containers


_CONTAINERS: Dict[str, Tuple[ExpectedEntry, ...]] = _build_containers()


# --- Read-only filesystem probes -------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _list_dir(path: Path) -> Tuple[List[Path], Optional[str]]:
    """Sorted children of ``path``; ``([], reason)`` when it cannot be listed."""
    try:
        return sorted(path.iterdir(), key=lambda child: child.name), None
    except OSError as exc:
        return [], f"{type(exc).__name__}: {exc}"


def _describe(path: Path) -> Dict[str, Any]:
    """Type facts about ``path`` without following into or touching it."""
    facts: Dict[str, Any] = {"kind": "unknown", "is_symlink": False, "symlink_target": None}
    try:
        facts["is_symlink"] = path.is_symlink()
    except OSError:
        return facts
    if facts["is_symlink"]:
        try:
            facts["symlink_target"] = os.readlink(path)
        except OSError:
            pass
    try:
        if path.is_dir():
            facts["kind"] = "dir"
        elif path.is_file():
            facts["kind"] = "file"
        else:
            facts["kind"] = "broken_symlink" if facts["is_symlink"] else "other"
    except OSError:
        facts["kind"] = "unknown"
    return facts


def _exists(path: Path) -> bool:
    try:
        return path.exists() or path.is_symlink()
    except OSError:
        return False


def _resolve_root(explicit: Any, resolver) -> Path:
    if explicit is None:
        return resolver()
    return Path(explicit).expanduser()


# --- The audit --------------------------------------------------------------


def _classify_top_level(child: Path) -> Dict[str, Any]:
    facts = _describe(child)
    record: Dict[str, Any] = {
        "path": child.name,
        "name": child.name,
        "container": "",
        "depth": 1,
        "kind": facts["kind"],
        "is_symlink": facts["is_symlink"],
        "symlink_target": facts["symlink_target"],
        "classification": CLASSIFICATION_DRIFT,
        "layout_generation": LAYOUT_UNKNOWN,
        "bucket": None,
        "canonical_target": None,
        "expected_kind": None,
        "kind_mismatch": False,
        "note": "",
    }
    bucket = _BUCKET_BY_NAME.get(child.name)
    if bucket is not None:
        record.update(
            classification=CLASSIFICATION_CANONICAL,
            layout_generation=LAYOUT_UNIFIED,
            bucket=bucket.name,
            expected_kind="dir",
            note=bucket.purpose,
        )
    else:
        legacy = _LEGACY_BY_NAME.get(child.name)
        if legacy is not None:
            record.update(
                classification=CLASSIFICATION_LEGACY,
                layout_generation=LAYOUT_PRE_PHASE_2,
                canonical_target=legacy.canonical_target,
                bucket=(
                    legacy.canonical_target.split("/", 1)[0]
                    if legacy.canonical_target
                    else None
                ),
                expected_kind=legacy.kind,
                note=legacy.note
                or (
                    f"pre-Phase-2 location; §4 target is {legacy.canonical_target}"
                    if legacy.canonical_target
                    else "recognised current state with no §4 destination"
                ),
            )
        else:
            record["note"] = "not named by the unified or the pre-Phase-2 layout"
    if record["expected_kind"] and record["kind"] not in {record["expected_kind"], "unknown"}:
        record["kind_mismatch"] = True
    return record


def _classify_child(
    child: Path, container: str, allowed: Tuple[ExpectedEntry, ...]
) -> Dict[str, Any]:
    facts = _describe(child)
    expected = {entry.name: entry for entry in allowed}.get(child.name)
    record: Dict[str, Any] = {
        "path": f"{container}/{child.name}",
        "name": child.name,
        "container": container,
        "depth": container.count("/") + 2,
        "kind": facts["kind"],
        "is_symlink": facts["is_symlink"],
        "symlink_target": facts["symlink_target"],
        "classification": CLASSIFICATION_DRIFT,
        "layout_generation": LAYOUT_UNKNOWN,
        "bucket": container.split("/", 1)[0],
        "canonical_target": None,
        "expected_kind": None,
        "kind_mismatch": False,
        "note": f"not an enumerated entry of {container}/",
    }
    if expected is not None:
        record.update(
            classification=CLASSIFICATION_CANONICAL,
            layout_generation=LAYOUT_UNIFIED,
            expected_kind=expected.kind,
            note=expected.purpose,
        )
        if record["kind"] not in {expected.kind, "unknown"}:
            record["kind_mismatch"] = True
    return record


def _missing_expected(root: Path) -> List[Dict[str, Any]]:
    """Canonical paths the spec enumerates that are absent from ``root``.

    A missing canonical path whose datum still sits in its pre-migration
    location is reported with ``legacy_present`` set, so a mid-migration tree
    reads as "Phase 2 has not run yet" rather than "data lost".
    """
    missing: List[Dict[str, Any]] = []
    legacy_source: Dict[str, str] = {
        entry.canonical_target: entry.name
        for entry in LEGACY_ROOT_ENTRIES
        if entry.canonical_target
    }
    for bucket in CANONICAL_BUCKETS:
        bucket_path = root / bucket.name
        bucket_present = _exists(bucket_path)
        if not bucket_present:
            missing.append(
                {
                    "path": bucket.name,
                    "bucket": bucket.name,
                    "kind": "dir",
                    "required": True,
                    "legacy_present": False,
                    "legacy_path": None,
                    "note": bucket.purpose,
                }
            )
        for entry in bucket.entries:
            relative = f"{bucket.name}/{entry.name}"
            if bucket_present and _exists(bucket_path / entry.name):
                continue
            legacy_name = legacy_source.get(relative)
            legacy_present = bool(legacy_name) and _exists(root / legacy_name)
            missing.append(
                {
                    "path": relative,
                    "bucket": bucket.name,
                    "kind": entry.kind,
                    "required": entry.required,
                    "legacy_present": legacy_present,
                    "legacy_path": legacy_name if legacy_present else None,
                    "note": entry.purpose,
                }
            )
    return missing


def _layout_generation(entries: List[Dict[str, Any]]) -> str:
    generations = {
        record["layout_generation"]
        for record in entries
        if record["layout_generation"] != LAYOUT_UNKNOWN
    }
    if generations == {LAYOUT_UNIFIED}:
        return LAYOUT_UNIFIED
    if generations == {LAYOUT_PRE_PHASE_2}:
        return LAYOUT_PRE_PHASE_2
    if len(generations) > 1:
        return "mixed"
    return LAYOUT_UNKNOWN


def audit_mac_home(
    root: Any = None,
    *,
    gateway_home: Any = None,
    openclaw_home: Any = None,
) -> Dict[str, Any]:
    """Audit the unified ``$MAC_HOME`` root and return a ``mac.mac_home_audit.v1`` report.

    Parameters are optional and exist for testability; with all of them omitted
    every root resolves through :mod:`mac.mac_paths`, which is the only
    sanctioned resolver.  The audit performs no writes of any kind and raises
    nothing: a missing or unreadable root is reported in ``status``.

    The returned mapping always carries the ``duplicates`` and ``orphans`` keys
    (empty here) so that the sibling duplicate-datum / orphan detectors can
    populate them without changing the schema.
    """
    root_path = _resolve_root(root, mac_paths.mac_home)
    gateway_path = _resolve_root(gateway_home, mac_paths.gateway_home)
    openclaw_path = _resolve_root(openclaw_home, mac_paths.openclaw_home)

    report: Dict[str, Any] = {
        "schema": MAC_HOME_AUDIT_SCHEMA,
        "root_path": str(root_path),
        "audited_at": _utc_now(),
        "root_exists": False,
        "status": STATUS_MISSING_ROOT,
        "status_detail": "",
        "layout_generation": LAYOUT_UNKNOWN,
        "roots": {
            "mac_home": str(root_path),
            "gateway_home": str(gateway_path),
            "openclaw_home": str(openclaw_path),
            "gateway_under_mac_home": _is_relative_to(gateway_path, root_path),
            "openclaw_under_mac_home": _is_relative_to(openclaw_path, root_path),
        },
        "buckets": [
            {
                "name": bucket.name,
                "purpose": bucket.purpose,
                "expected_entries": [
                    {
                        "name": entry.name,
                        "kind": entry.kind,
                        "required": entry.required,
                        "purpose": entry.purpose,
                    }
                    for entry in bucket.entries
                ],
                "present": False,
            }
            for bucket in CANONICAL_BUCKETS
        ],
        "entries": [],
        "canonical": [],
        "legacy_accepted": [],
        "drift": [],
        "missing_expected": [],
        "unreadable_paths": [],
        # Reserved for the sibling detectors; present so the schema is stable.
        "duplicates": [],
        "orphans": [],
        "summary": {},
    }

    if not _exists(root_path):
        report["status_detail"] = "root does not exist"
        report["summary"] = _summarize(report)
        return report

    report["root_exists"] = True
    try:
        is_dir = root_path.is_dir()
    except OSError as exc:
        report["status"] = STATUS_UNREADABLE_ROOT
        report["status_detail"] = f"{type(exc).__name__}: {exc}"
        report["summary"] = _summarize(report)
        return report
    if not is_dir:
        report["status"] = STATUS_NOT_A_DIRECTORY
        report["status_detail"] = "root exists but is not a directory"
        report["summary"] = _summarize(report)
        return report

    children, error = _list_dir(root_path)
    if error is not None:
        report["status"] = STATUS_UNREADABLE_ROOT
        report["status_detail"] = error
        report["unreadable_paths"].append({"path": "", "error": error})
        report["summary"] = _summarize(report)
        return report

    report["status"] = STATUS_OK
    entries: List[Dict[str, Any]] = []
    for child in children:
        record = _classify_top_level(child)
        entries.append(record)
        allowed = container_allow_list(child.name)
        if allowed is not None and record["kind"] == "dir":
            entries.extend(
                _scan_container(child, child.name, allowed, report["unreadable_paths"])
            )

    # A canonical bucket may itself hold a descend-able container (gateway/openclaw).
    for record in list(entries):
        if record["depth"] != 2:
            continue
        allowed = container_allow_list(record["path"])
        if allowed is None or record["kind"] != "dir":
            continue
        entries.extend(
            _scan_container(
                root_path / record["path"],
                record["path"],
                allowed,
                report["unreadable_paths"],
            )
        )

    entries.sort(key=lambda record: (record["depth"], record["path"]))
    report["entries"] = entries
    report["canonical"] = [
        r["path"] for r in entries if r["classification"] == CLASSIFICATION_CANONICAL
    ]
    report["legacy_accepted"] = [
        r["path"] for r in entries if r["classification"] == CLASSIFICATION_LEGACY
    ]
    report["drift"] = [r["path"] for r in entries if r["classification"] == CLASSIFICATION_DRIFT]
    report["missing_expected"] = _missing_expected(root_path)
    report["layout_generation"] = _layout_generation(entries)
    for bucket_report in report["buckets"]:
        bucket_report["present"] = _exists(root_path / bucket_report["name"])
    report["summary"] = _summarize(report)
    return report


def _scan_container(
    path: Path,
    relative: str,
    allowed: Tuple[ExpectedEntry, ...],
    unreadable: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    children, error = _list_dir(path)
    if error is not None:
        unreadable.append({"path": relative, "error": error})
        return []
    return [_classify_child(child, relative, allowed) for child in children]


def _is_relative_to(path: Path, other: Path) -> bool:
    try:
        return path.is_relative_to(other)
    except (OSError, ValueError):
        return False


def _summarize(report: Dict[str, Any]) -> Dict[str, Any]:
    entries: List[Dict[str, Any]] = report["entries"]
    missing: List[Dict[str, Any]] = report["missing_expected"]
    return {
        "status": report["status"],
        "root_exists": report["root_exists"],
        "layout_generation": report["layout_generation"],
        "entries": len(entries),
        "top_level_entries": sum(1 for r in entries if r["depth"] == 1),
        "canonical": len(report["canonical"]),
        "legacy_accepted": len(report["legacy_accepted"]),
        "drift": len(report["drift"]),
        "kind_mismatches": sum(1 for r in entries if r["kind_mismatch"]),
        "buckets_present": sum(1 for b in report["buckets"] if b["present"]),
        "buckets_expected": len(CANONICAL_BUCKETS),
        "missing_expected": len(missing),
        "missing_required": sum(1 for m in missing if m["required"]),
        "missing_with_legacy_source": sum(1 for m in missing if m["legacy_present"]),
        "unreadable_paths": len(report["unreadable_paths"]),
        # Reserved counters for the sibling detectors.
        "duplicates": len(report["duplicates"]),
        "orphans": len(report["orphans"]),
    }
