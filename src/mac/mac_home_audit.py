"""Strictly READ-ONLY auditor of the unified ``$MAC_HOME`` root.

This module inspects the consolidated MAC home laid out in
``docs/home-consolidation.md`` §4 and reports how faithfully the on-disk tree
matches the approved canonical shape. It is the generalization of the earlier,
gateway-only ``hermes_home_audit`` (which was deleted as dead code — see the
consolidation doc) onto the single authoritative root.

The auditor is deliberately conservative:

  * It NEVER creates, writes, chmods, or deletes anything. Every filesystem
    call is a read (``exists``/``is_dir``/``iterdir``/``stat``). A missing or
    unreadable root is reported in ``root_status`` / ``root_exists``, never
    raised.
  * The fleet is mid-migration, so the spec accepts BOTH the target unified
    layout (``ledger/``, ``secrets/``, ``fleet/``, ``runtime/``, ``gateway/``,
    ``toolchain/``) AND today's pre-Phase-2 flat root (``mac.db``, ``mac.env``,
    ``fleets.yaml``, ``openclaw/``, ``journal/``, ``backups/`` … directly at the
    root). Every observed entry is classified as ``canonical``,
    ``legacy_accepted`` (a recognised pre-migration location, with its canonical
    target named), or ``drift`` (unknown).
  * Every root is resolved through :mod:`mac.mac_paths` only; no new
    hard-coded ``.mac`` / ``.hermes`` home literals appear here (enforced by
    ``tests/test_mac_paths_no_hardcode.py``).

Duplicate-datum and orphan detection are owned by a sibling task; the schema
reserves stable keys for them (``duplicates``, ``orphans``) so consumers can
depend on the shape now.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mac import mac_paths

__all__ = [
    "MAC_HOME_AUDIT_SCHEMA",
    "CANONICAL_LAYOUT",
    "TopLevelSpec",
    "EntryClassification",
    "audit_mac_home",
]

#: Stable schema string for the report dict emitted by :func:`audit_mac_home`.
MAC_HOME_AUDIT_SCHEMA = "mac.mac_home_audit.v1"

#: Classification generations an observed entry can belong to.
CANONICAL = "canonical"
LEGACY_ACCEPTED = "legacy_accepted"
DRIFT = "drift"


@dataclass(frozen=True)
class ExpectedChild:
    """A named entry expected inside a canonical top-level container dir."""

    name: str
    kind: str  # "dir" | "file"


@dataclass(frozen=True)
class TopLevelSpec:
    """One canonical top-level bucket of the unified ``$MAC_HOME`` root.

    ``children`` enumerates the entries that legitimately live one level deeper
    inside a well-known container dir, so the auditor can flag non-standard
    entries there the same way it flags non-standard top-level paths.
    """

    name: str
    kind: str  # "dir" | "file"
    description: str = ""
    children: tuple[ExpectedChild, ...] = ()

    def child_names(self) -> set[str]:
        return {child.name for child in self.children}


@dataclass(frozen=True)
class LegacyLocation:
    """A recognised pre-Phase-2 on-disk entry and its canonical target.

    ``canonical_target`` is the slash-joined path (relative to the root) the
    entry is expected to move to once the consolidation completes, e.g.
    ``mac.db`` → ``ledger/mac.db``.
    """

    name: str
    kind: str  # "dir" | "file"
    canonical_target: str


# --- Canonical unified layout (docs/home-consolidation.md §4) --------------
#
# Modelled as data, NOT ad-hoc conditionals, so the detectors and the tests can
# consume the same single source of truth.

CANONICAL_LAYOUT: tuple[TopLevelSpec, ...] = (
    TopLevelSpec(
        name="ledger",
        kind="dir",
        description="mac.db ledger + backups + archive",
        children=(
            ExpectedChild("mac.db", "file"),
            ExpectedChild("backups", "dir"),
            ExpectedChild("archive", "dir"),
        ),
    ),
    TopLevelSpec(
        name="secrets",
        kind="dir",
        description="single secret source",
        children=(
            ExpectedChild("mac.env", "file"),
            ExpectedChild(".env", "file"),
            ExpectedChild("client-principals.json", "file"),
        ),
    ),
    TopLevelSpec(
        name="fleet",
        kind="dir",
        description="fleet registry + specs",
        children=(
            ExpectedChild("fleets.yaml", "file"),
            ExpectedChild("specs", "dir"),
        ),
    ),
    TopLevelSpec(
        name="runtime",
        kind="dir",
        description="mac runtime context + memory topology + journal",
        children=(
            ExpectedChild("mac-runtime-context.json", "file"),
            ExpectedChild("mac-runtime-context.md", "file"),
            ExpectedChild("mac-memory-topology.json", "file"),
            ExpectedChild("journal", "dir"),
        ),
    ),
    TopLevelSpec(
        name="gateway",
        kind="dir",
        description="today's ~/.hermes: soul, memory, sessions, skills, cron, dream logs",
        children=(
            ExpectedChild("openclaw", "dir"),
        ),
    ),
    TopLevelSpec(
        name="toolchain",
        kind="dir",
        description="installed source, venv, bin, hermes-agent",
        children=(
            ExpectedChild("src", "dir"),
            ExpectedChild("venv", "dir"),
            ExpectedChild("bin", "dir"),
            ExpectedChild("hermes-agent", "dir"),
        ),
    ),
)


# --- Legacy (pre-Phase-2) flat-root allow-list -----------------------------
#
# Today the root is flat: ledger/secrets/fleet/runtime/toolchain do not exist
# yet, and their contents sit directly under $MAC_HOME. These entries are
# recognised (``legacy_accepted``) rather than flagged as drift, and each names
# the canonical target it will migrate to.

LEGACY_LAYOUT: tuple[LegacyLocation, ...] = (
    LegacyLocation("mac.db", "file", "ledger/mac.db"),
    LegacyLocation("backups", "dir", "ledger/backups"),
    LegacyLocation("archive", "dir", "ledger/archive"),
    LegacyLocation("mac.env", "file", "secrets/mac.env"),
    LegacyLocation(".env", "file", "secrets/.env"),
    LegacyLocation("client-principals.json", "file", "secrets/client-principals.json"),
    LegacyLocation("fleets.yaml", "file", "fleet/fleets.yaml"),
    LegacyLocation("specs", "dir", "fleet/specs"),
    LegacyLocation("mac-runtime-context.json", "file", "runtime/mac-runtime-context.json"),
    LegacyLocation("mac-runtime-context.md", "file", "runtime/mac-runtime-context.md"),
    LegacyLocation("mac-memory-topology.json", "file", "runtime/mac-memory-topology.json"),
    LegacyLocation("journal", "dir", "runtime/journal"),
    LegacyLocation("openclaw", "dir", "gateway/openclaw"),
    LegacyLocation("src", "dir", "toolchain/src"),
    LegacyLocation("venv", "dir", "toolchain/venv"),
    LegacyLocation("bin", "dir", "toolchain/bin"),
    LegacyLocation("hermes-agent", "dir", "toolchain/hermes-agent"),
    # qdrant/ (L2 memory) is an authoritative .mac artifact today with no
    # separately-named target bucket; recognise it so it is not called drift.
    LegacyLocation("qdrant", "dir", "qdrant"),
)


@dataclass
class EntryClassification:
    """How one observed top-level (or nested) entry maps onto the spec."""

    name: str
    path: str
    kind: str  # "dir" | "file" | "other"
    classification: str  # canonical | legacy_accepted | drift
    generation: str  # "canonical" | "legacy" | "unknown"
    canonical_target: str | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "classification": self.classification,
            "generation": self.generation,
            "canonical_target": self.canonical_target,
            "detail": self.detail,
        }


@dataclass
class _AuditState:
    entries: list[EntryClassification] = field(default_factory=list)
    missing_expected: list[dict[str, object]] = field(default_factory=list)
    nonstandard_nested: list[dict[str, object]] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_kind(path: Path) -> str:
    try:
        if path.is_dir():
            return "dir"
        if path.is_file():
            return "file"
    except OSError:
        return "other"
    return "other"


def _canonical_index() -> dict[str, TopLevelSpec]:
    return {spec.name: spec for spec in CANONICAL_LAYOUT}


def _legacy_index() -> dict[str, LegacyLocation]:
    return {loc.name: loc for loc in LEGACY_LAYOUT}


def _classify_nested(spec: TopLevelSpec, container: Path) -> list[dict[str, object]]:
    """Flag non-standard entries one level inside a canonical container dir."""

    expected = spec.child_names()
    findings: list[dict[str, object]] = []
    try:
        children = sorted(container.iterdir(), key=lambda p: p.name)
    except OSError:
        return findings
    for child in children:
        if child.name in expected:
            continue
        findings.append(
            {
                "container": spec.name,
                "name": child.name,
                "path": str(child),
                "kind": _entry_kind(child),
                "classification": DRIFT,
            }
        )
    return findings


def audit_mac_home(
    root: Path | str | None = None,
    *,
    now: str | None = None,
) -> dict[str, object]:
    """Audit the unified ``$MAC_HOME`` root and return a report dict.

    Parameters
    ----------
    root:
        Explicit root path for testability. When ``None`` the root is resolved
        through :func:`mac.mac_paths.mac_home`.
    now:
        Optional ISO-8601 UTC timestamp override (tests pin this for
        determinism). Defaults to the current UTC time.

    The returned dict conforms to schema :data:`MAC_HOME_AUDIT_SCHEMA`. A
    missing or unreadable root is reported via ``root_exists`` / ``root_status``
    and yields empty entry lists; it is never raised.
    """

    root_path = Path(root).expanduser() if root is not None else mac_paths.mac_home()
    audited_at = now or _now_iso()

    root_exists = False
    root_status = "missing"
    try:
        root_exists = root_path.exists()
        if root_exists:
            root_status = "ok" if root_path.is_dir() else "not_a_directory"
    except OSError as exc:  # unreadable parent, permission error, etc.
        root_status = f"unreadable: {exc.__class__.__name__}"

    state = _AuditState()
    canonical_index = _canonical_index()
    legacy_index = _legacy_index()

    top_names: list[str] = []
    if root_exists and root_status == "ok":
        try:
            top_names = [entry.name for entry in sorted(root_path.iterdir(), key=lambda p: p.name)]
        except OSError as exc:
            root_status = f"unreadable: {exc.__class__.__name__}"
            top_names = []

    for name in top_names:
        entry_path = root_path / name
        kind = _entry_kind(entry_path)
        if name in canonical_index:
            spec = canonical_index[name]
            state.entries.append(
                EntryClassification(
                    name=name,
                    path=str(entry_path),
                    kind=kind,
                    classification=CANONICAL,
                    generation="canonical",
                    detail=spec.description,
                )
            )
            if kind == "dir":
                state.nonstandard_nested.extend(_classify_nested(spec, entry_path))
        elif name in legacy_index:
            loc = legacy_index[name]
            state.entries.append(
                EntryClassification(
                    name=name,
                    path=str(entry_path),
                    kind=kind,
                    classification=LEGACY_ACCEPTED,
                    generation="legacy",
                    canonical_target=loc.canonical_target,
                    detail=f"recognised pre-migration location; canonical target {loc.canonical_target}",
                )
            )
        else:
            state.entries.append(
                EntryClassification(
                    name=name,
                    path=str(entry_path),
                    kind=kind,
                    classification=DRIFT,
                    generation="unknown",
                    detail="non-standard top-level entry",
                )
            )

    # Missing expected paths: canonical top-level buckets absent from disk. In
    # the pre-Phase-2 flat root these are expected to be absent, so annotate
    # whether a legacy stand-in for the bucket's contents is present.
    present = set(top_names)
    for spec in CANONICAL_LAYOUT:
        if spec.name in present:
            continue
        legacy_present = sorted(
            loc.name
            for loc in LEGACY_LAYOUT
            if loc.canonical_target.startswith(spec.name + "/") and loc.name in present
        )
        state.missing_expected.append(
            {
                "name": spec.name,
                "kind": spec.kind,
                "canonical_path": str(root_path / spec.name),
                "legacy_present": legacy_present,
            }
        )

    entries_dicts = [entry.to_dict() for entry in state.entries]
    summary = {
        "total_entries": len(entries_dicts),
        "canonical": sum(1 for e in entries_dicts if e["classification"] == CANONICAL),
        "legacy_accepted": sum(1 for e in entries_dicts if e["classification"] == LEGACY_ACCEPTED),
        "drift": sum(1 for e in entries_dicts if e["classification"] == DRIFT),
        "missing_expected": len(state.missing_expected),
        "nonstandard_nested": len(state.nonstandard_nested),
    }

    return {
        "schema": MAC_HOME_AUDIT_SCHEMA,
        "root_path": str(root_path),
        "audited_at": audited_at,
        "root_exists": root_exists,
        "root_status": root_status,
        "entries": entries_dicts,
        "missing_expected": state.missing_expected,
        "nonstandard_nested": state.nonstandard_nested,
        # Reserved for the sibling duplicate/orphan task; stable keys now.
        "duplicates": [],
        "orphans": [],
        "summary": summary,
    }
