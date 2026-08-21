"""Strictly read-only auditor of the unified MAC home root.

`docs/home-consolidation.md` §4 approves a single authoritative root with six
top-level buckets — ``ledger/``, ``secrets/``, ``fleet/``, ``runtime/``,
``gateway/`` (which contains ``openclaw/``) and ``toolchain/``. The fleet is
mid-migration: on disk today the same data still sits *flat* at the root
(``mac.db``, ``mac.env``, ``fleets.yaml``, ``openclaw/``, ``journal/``,
``backups/`` …), and Phases 1–3 have not run.

This module answers "does this root match the plan, and if not, how?" without
ever touching the tree. It is the generalisation of the deleted
``hermes_home_audit`` (see §5 "Cross-cutting"): the layout is expressed once as
DATA — :data:`CANONICAL_LAYOUT` plus :data:`LEGACY_ROOT_ENTRIES` — so the
classifier, the drift detectors, the sibling duplicate/orphan detectors and the
tests all read the same spec instead of re-deriving it from conditionals.

Every observed entry gets exactly one classification:

``canonical``
    It is where §4 says it belongs (a bucket, or an expected entry inside one).
``legacy_accepted``
    A recognised pre-Phase-2 location. The report names its canonical target
    when §4 assigns one, and says so explicitly when §4 does not yet place it.
``drift``
    Nothing in the spec recognises it. This is the abandoned-metadata signal.

Contract:

* Read-only. The module only calls ``exists``/``is_dir``/``is_file``/
  ``iterdir``/``lstat``. It never creates, writes, chmods, moves or deletes.
* Total. A missing, non-directory or unreadable root is reported in
  ``status``; an unreadable subdirectory is reported per-path. Nothing raises.
* Roots resolve only through :mod:`mac.mac_paths`, so relocation keeps working
  and ``tests/test_mac_paths_no_hardcode.py`` stays satisfied. Callers may pass
  an explicit root for testing.

Duplicate-datum and orphan detection are a sibling task; their report keys are
reserved here (always present, always empty) so `mac.mac_home_audit.v1` is
stable the moment they land.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

from mac import mac_paths

MAC_HOME_AUDIT_SCHEMA = "mac.mac_home_audit.v1"

# Classifications. One per observed entry; the report also groups by these.
CANONICAL = "canonical"
LEGACY_ACCEPTED = "legacy_accepted"
DRIFT = "drift"

# Layout generations. `pre_phase2` is today's flat root; `unified` is §4.
GENERATION_UNIFIED = "unified"
GENERATION_PRE_PHASE2 = "pre_phase2"
GENERATION_UNKNOWN = "unknown"

# Root-level status. Everything except `ok` means the walk produced no entries.
STATUS_OK = "ok"
STATUS_MISSING = "missing_root"
STATUS_NOT_A_DIRECTORY = "not_a_directory"
STATUS_UNREADABLE = "unreadable_root"

DIR = "dir"
FILE = "file"


# --------------------------------------------------------------------------
# The spec: docs/home-consolidation.md §4 expressed as data
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExpectedPath:
    """One entry §4 places inside a bucket, e.g. ``ledger/mac.db``."""

    name: str
    kind: str
    purpose: str
    # Where the same datum lives BEFORE the migration, relative to the root.
    # ``None`` means §4 introduces it and there is no pre-migration location.
    legacy_location: str | None = None


@dataclass(frozen=True)
class Bucket:
    """One §4 top-level bucket and the entries it is expected to contain."""

    name: str
    purpose: str
    contents: tuple[ExpectedPath, ...] = ()

    def expected_names(self) -> frozenset[str]:
        return frozenset(item.name for item in self.contents)


@dataclass(frozen=True)
class LegacyRootEntry:
    """A recognised entry that still sits directly at the root today.

    ``canonical_target`` is the §4 destination relative to the root, or ``None``
    for state that §4 does not (yet) place. ``None`` is deliberate: inventing a
    destination the approved plan never named would make this auditor a second,
    competing source of truth. Reporting "recognised, unplaced" is the honest
    answer and is exactly the input the Phase-1/2 planning needs.
    """

    name: str
    kind: str
    canonical_target: str | None
    # Why this name is recognised: the first-party site that resolves it.
    evidence: str


# §4's tree. Bucket order is the documented order.
CANONICAL_LAYOUT: tuple[Bucket, ...] = (
    Bucket(
        name="ledger",
        purpose="Hub ledger database and its backup/archive artifacts.",
        contents=(
            ExpectedPath("mac.db", FILE, "Hub ledger database.", "mac.db"),
            ExpectedPath("backups", DIR, "Ledger backups and deploy rollback artifacts.", "backups"),
            ExpectedPath("archive", DIR, "Ledger archive.", "archive"),
        ),
    ),
    Bucket(
        name="secrets",
        purpose="The single secret source; 0700 is what preserves the trust boundary.",
        contents=(
            ExpectedPath("mac.env", FILE, "Hub/service secrets.", "mac.env"),
            ExpectedPath(".env", FILE, "Client deploy env (scoped fleet tokens).", ".env"),
            ExpectedPath(
                "client-principals.json",
                FILE,
                "Client principal registry.",
                "client-principals.json",
            ),
        ),
    ),
    Bucket(
        name="fleet",
        purpose="Fleet registry and per-fleet specs.",
        contents=(
            ExpectedPath("fleets.yaml", FILE, "Fleet registry.", "fleets.yaml"),
            ExpectedPath("specs", DIR, "Per-fleet specs.", "specs"),
        ),
    ),
    Bucket(
        name="runtime",
        purpose="Control-plane runtime state, including what Phase 1 pulls back out of the gateway.",
        contents=(
            # These three are written into the GATEWAY home today
            # (deploy_env.py:437-443) -- reverse leakage, §2 item 3. Phase 1
            # moves them here, so there is no pre-migration location at the
            # root and `legacy_location` stays None.
            ExpectedPath("mac-runtime-context.json", FILE, "Runtime context (machine-readable)."),
            ExpectedPath("mac-runtime-context.md", FILE, "Runtime context (operator-readable)."),
            ExpectedPath("mac-memory-topology.json", FILE, "Memory topology snapshot."),
            ExpectedPath("journal", DIR, "Dated soul/memory snapshots.", "journal"),
        ),
    ),
    Bucket(
        name="gateway",
        purpose="The agent-personal home (today's separate gateway root).",
        contents=(
            ExpectedPath("openclaw", DIR, "OpenClaw gateway home.", "openclaw"),
        ),
    ),
    Bucket(
        name="toolchain",
        purpose="Installed source, virtualenv and entry points.",
        contents=(
            ExpectedPath("src", DIR, "Installed repository checkouts.", "src"),
            ExpectedPath("venv", DIR, "Runtime virtualenv.", "venv"),
            ExpectedPath("bin", DIR, "Installed entry points.", "bin"),
            ExpectedPath("hermes-agent", DIR, "Legacy gateway agent install.", "hermes-agent"),
        ),
    ),
)

BUCKET_NAMES: frozenset[str] = frozenset(bucket.name for bucket in CANONICAL_LAYOUT)

# Today's flat root. Every entry is grounded in a first-party resolver so this
# list is auditable rather than remembered; `evidence` names that site.
LEGACY_ROOT_ENTRIES: tuple[LegacyRootEntry, ...] = (
    LegacyRootEntry("mac.db", FILE, "ledger/mac.db", "mac_paths.ledger_db"),
    LegacyRootEntry("backups", DIR, "ledger/backups", "mac_paths.backups_dir"),
    LegacyRootEntry("archive", DIR, "ledger/archive", "mac_paths.archive_dir"),
    LegacyRootEntry("mac.env", FILE, "secrets/mac.env", "mac_paths.mac_env_file"),
    LegacyRootEntry(".env", FILE, "secrets/.env", "mac_paths.deploy_env_file"),
    LegacyRootEntry(
        "client-principals.json",
        FILE,
        "secrets/client-principals.json",
        "client_principals.principals_path",
    ),
    LegacyRootEntry("fleets.yaml", FILE, "fleet/fleets.yaml", "mac_paths.fleets_config"),
    LegacyRootEntry("specs", DIR, "fleet/specs", "docs/home-consolidation.md §4"),
    LegacyRootEntry("journal", DIR, "runtime/journal", "mac_paths.journal_dir"),
    LegacyRootEntry("openclaw", DIR, "gateway/openclaw", "mac_paths.openclaw_home"),
    LegacyRootEntry("src", DIR, "toolchain/src", "worker.repository_checkout_candidates"),
    LegacyRootEntry("venv", DIR, "toolchain/venv", "worker_runtime_deps.runtime_python"),
    LegacyRootEntry("bin", DIR, "toolchain/bin", "acp.backend agent entry point"),
    LegacyRootEntry(
        "hermes-agent", DIR, "toolchain/hermes-agent", "docs/home-consolidation.md §4"
    ),
    # Recognised, but §4 never assigned them a bucket. See LegacyRootEntry.
    LegacyRootEntry("qdrant", DIR, None, "docs/home-consolidation.md §1 (L2 memory)"),
    LegacyRootEntry("openshell", DIR, None, "openshell_supervisor.agent_policy_path"),
    LegacyRootEntry("openshell-policy.yaml", FILE, None, "executor_sandbox deployed policy"),
    LegacyRootEntry("public-artifacts", DIR, None, "hermes_runtime published artifact root"),
    LegacyRootEntry("sessions", DIR, None, "client_login.sessions_dir"),
    LegacyRootEntry("ssh", DIR, None, "client_login known-hosts store"),
    LegacyRootEntry("clients", DIR, None, "client_profiles.clients_dir"),
    LegacyRootEntry("credentials", DIR, None, "client_profiles client credential store"),
    LegacyRootEntry("agent-footprint.json", FILE, None, "worker_runtime_deps footprint"),
    LegacyRootEntry(
        "fleet-release-publication.lock",
        FILE,
        None,
        "fleet_release_epoch_service publication lock",
    ),
    LegacyRootEntry(".install.lock", FILE, None, "worker_runtime_deps install lock"),
)

LEGACY_ROOT_BY_NAME: dict[str, LegacyRootEntry] = {
    entry.name: entry for entry in LEGACY_ROOT_ENTRIES
}


def bucket(name: str) -> Bucket | None:
    """Return the §4 bucket called ``name``, or ``None``."""
    for candidate in CANONICAL_LAYOUT:
        if candidate.name == name:
            return candidate
    return None


def canonical_paths() -> tuple[str, ...]:
    """Every path §4 expects, relative to the root, buckets first.

    The sibling duplicate/orphan detectors consume this so they never
    re-enumerate the layout.
    """
    paths: list[str] = []
    for item in CANONICAL_LAYOUT:
        paths.append(item.name)
        paths.extend("%s/%s" % (item.name, entry.name) for entry in item.contents)
    return tuple(paths)


# --------------------------------------------------------------------------
# Read-only filesystem probes
# --------------------------------------------------------------------------


def _kind(path: Path) -> str:
    """``dir``/``file`` for ``path``, resolving symlinks like the callers do."""
    try:
        return DIR if path.is_dir() else FILE
    except OSError:
        return FILE


def _listdir(path: Path) -> tuple[list[Path], str | None]:
    """Sorted children of ``path``, or ``([], reason)`` when it cannot be read."""
    try:
        return sorted(path.iterdir(), key=lambda child: child.name), None
    except OSError as exc:
        return [], exc.strerror or exc.__class__.__name__


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _entry(
    *,
    relative: str,
    name: str,
    parent: str,
    path: Path,
    classification: str,
    generation: str,
    canonical_target: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    return {
        "path": relative,
        "name": name,
        "parent": parent,
        "kind": _kind(path),
        "classification": classification,
        "generation": generation,
        "canonical_target": canonical_target,
        "detail": detail,
    }


def _root_entries(root: Path) -> Iterator[dict[str, Any]]:
    """Classify the top level: the generalisation of the old allow-list."""
    children, _ = _listdir(root)
    for child in children:
        name = child.name
        if name in BUCKET_NAMES:
            yield _entry(
                relative=name,
                name=name,
                parent="",
                path=child,
                classification=CANONICAL,
                generation=GENERATION_UNIFIED,
                detail=bucket(name).purpose if bucket(name) else None,
            )
            continue
        legacy = LEGACY_ROOT_BY_NAME.get(name)
        if legacy is not None:
            yield _entry(
                relative=name,
                name=name,
                parent="",
                path=child,
                classification=LEGACY_ACCEPTED,
                generation=GENERATION_PRE_PHASE2,
                canonical_target=legacy.canonical_target,
                detail=(
                    "recognised pre-migration location (%s); §4 assigns no bucket"
                    % legacy.evidence
                    if legacy.canonical_target is None
                    else "recognised pre-migration location (%s)" % legacy.evidence
                ),
            )
            continue
        yield _entry(
            relative=name,
            name=name,
            parent="",
            path=child,
            classification=DRIFT,
            generation=GENERATION_UNKNOWN,
            detail="not named by the canonical layout or the pre-migration allow-list",
        )


def _bucket_entries(root: Path, unreadable: list[dict[str, str]]) -> Iterator[dict[str, Any]]:
    """One level inside each present bucket — the second drift surface."""
    for item in CANONICAL_LAYOUT:
        container = root / item.name
        try:
            if not container.is_dir():
                continue
        except OSError:
            continue
        children, problem = _listdir(container)
        if problem is not None:
            unreadable.append({"path": item.name, "error": problem})
            continue
        expected = item.expected_names()
        for child in children:
            relative = "%s/%s" % (item.name, child.name)
            if child.name in expected:
                yield _entry(
                    relative=relative,
                    name=child.name,
                    parent=item.name,
                    path=child,
                    classification=CANONICAL,
                    generation=GENERATION_UNIFIED,
                )
                continue
            yield _entry(
                relative=relative,
                name=child.name,
                parent=item.name,
                path=child,
                classification=DRIFT,
                generation=GENERATION_UNKNOWN,
                detail="not an expected entry of the %s bucket" % item.name,
            )


def _missing_expected(root: Path) -> list[dict[str, Any]]:
    """Canonical paths that are absent, and whether the legacy copy is present."""
    missing: list[dict[str, Any]] = []
    for item in CANONICAL_LAYOUT:
        bucket_present = _exists(root / item.name)
        if not bucket_present:
            missing.append(
                {
                    "path": item.name,
                    "kind": DIR,
                    "purpose": item.purpose,
                    "legacy_location": None,
                    "legacy_present": False,
                }
            )
        for entry in item.contents:
            relative = "%s/%s" % (item.name, entry.name)
            if bucket_present and _exists(root / item.name / entry.name):
                continue
            legacy_present = bool(
                entry.legacy_location is not None and _exists(root / entry.legacy_location)
            )
            missing.append(
                {
                    "path": relative,
                    "kind": entry.kind,
                    "purpose": entry.purpose,
                    "legacy_location": entry.legacy_location,
                    "legacy_present": legacy_present,
                }
            )
    return missing


def _generation(entries: Iterable[dict[str, Any]]) -> str:
    """Which layout generation the observed ROOT belongs to.

    Only top-level entries decide this: a `pre_phase2` root that also happens to
    contain a stray `runtime/` is `mixed`, which is precisely the mid-migration
    state operators need to see.
    """
    unified = False
    legacy = False
    for entry in entries:
        if entry["parent"]:
            continue
        if entry["classification"] == CANONICAL:
            unified = True
        elif entry["classification"] == LEGACY_ACCEPTED:
            legacy = True
    if unified and legacy:
        return "mixed"
    if unified:
        return GENERATION_UNIFIED
    if legacy:
        return GENERATION_PRE_PHASE2
    return GENERATION_UNKNOWN


def _root_status(root: Path) -> tuple[str, bool, str | None]:
    """``(status, root_exists, detail)`` — never raises, never hard-fails."""
    try:
        if not root.exists():
            return STATUS_MISSING, False, "no such path"
        if not root.is_dir():
            return STATUS_NOT_A_DIRECTORY, True, "path exists but is not a directory"
    except OSError as exc:
        return STATUS_UNREADABLE, False, exc.strerror or exc.__class__.__name__
    _, problem = _listdir(root)
    if problem is not None:
        return STATUS_UNREADABLE, True, problem
    return STATUS_OK, True, None


def _report(
    root: Path,
    *,
    status: str,
    root_exists: bool,
    detail: str | None,
    entries: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    unreadable: list[dict[str, str]],
) -> dict[str, Any]:
    by_class = {
        CANONICAL: [entry["path"] for entry in entries if entry["classification"] == CANONICAL],
        LEGACY_ACCEPTED: [
            entry["path"] for entry in entries if entry["classification"] == LEGACY_ACCEPTED
        ],
        DRIFT: [entry["path"] for entry in entries if entry["classification"] == DRIFT],
    }
    return {
        "schema": MAC_HOME_AUDIT_SCHEMA,
        "root_path": str(root),
        "audited_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "root_exists": root_exists,
        "status": status,
        "status_detail": detail,
        "layout": {
            "generation_detected": _generation(entries),
            "buckets": [item.name for item in CANONICAL_LAYOUT],
        },
        "entries": entries,
        "canonical": by_class[CANONICAL],
        "legacy_accepted": by_class[LEGACY_ACCEPTED],
        "drift": by_class[DRIFT],
        "missing_expected": missing,
        "unreadable": unreadable,
        # Reserved for the sibling duplicate/orphan detectors. Present and empty
        # so consumers of mac.mac_home_audit.v1 need no schema change later.
        "duplicates": [],
        "orphans": [],
        "summary": {
            "entries": len(entries),
            "canonical": len(by_class[CANONICAL]),
            "legacy_accepted": len(by_class[LEGACY_ACCEPTED]),
            "drift": len(by_class[DRIFT]),
            "missing_expected": len(missing),
            "unreadable": len(unreadable),
            "duplicates": 0,
            "orphans": 0,
        },
    }


def audit_mac_home(root: Path | str | None = None) -> dict[str, Any]:
    """Audit the unified home root and return a `mac.mac_home_audit.v1` report.

    ``root`` defaults to :func:`mac.mac_paths.mac_home`, which is the only
    sanctioned resolver; pass an explicit path only for tests and tooling that
    already hold one. The call performs no writes and raises nothing: an absent,
    non-directory or unreadable root comes back as ``status``.
    """
    resolved = Path(root).expanduser() if root is not None else mac_paths.mac_home()
    status, root_exists, detail = _root_status(resolved)
    if status != STATUS_OK:
        return _report(
            resolved,
            status=status,
            root_exists=root_exists,
            detail=detail,
            entries=[],
            missing=[],
            unreadable=[],
        )
    unreadable: list[dict[str, str]] = []
    entries = list(_root_entries(resolved)) + list(_bucket_entries(resolved, unreadable))
    return _report(
        resolved,
        status=status,
        root_exists=root_exists,
        detail=detail,
        entries=entries,
        missing=_missing_expected(resolved),
        unreadable=unreadable,
    )
