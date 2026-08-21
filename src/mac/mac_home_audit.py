"""Read-only auditor for the unified MAC home root.

``docs/home-consolidation.md`` §4 approves ONE authoritative root with six
top-level buckets::

    <root>
    ├── ledger/    mac.db, backups, archive
    ├── secrets/   mac.env, .env, client-principals.json
    ├── fleet/     fleets.yaml, specs
    ├── runtime/   mac-runtime-context.*, memory topology, journal/
    ├── gateway/   (today's gateway home)
    │   └── openclaw/
    └── toolchain/ src, venv, bin, hermes-agent

That layout is modelled here as *data* (:data:`CANONICAL_BUCKETS`) rather than
as conditionals, so the classifier, the missing-path report, the sibling
duplicate/orphan detectors and the tests all consume one description of the
target shape instead of four drifting copies of it.

The fleet is mid-migration, so the audit must also make sense of the shape that
is actually on disk today: the ledger, the secrets file, the fleet registry,
``journal/``, ``backups/`` and the gateway tree still sit directly at the root.
Those are *recognised*, not wrong, so every observed entry is classified as one
of three things:

``canonical``
    A bucket named by §4 (or, one level down, a path §4 places in that bucket).
``legacy_accepted``
    A recognised pre-migration location. Where §4 says where the entry ends up,
    the report names that destination in ``canonical_target``; where the plan
    has not yet assigned it a bucket, ``canonical_target`` is ``None`` and the
    entry's ``detail`` says so. Reporting the gap honestly is the point — the
    alternative is inventing a destination the plan never approved.
``drift``
    Not recognised at all: at the root, or one level inside a canonical bucket.

Contract:

* **Strictly read-only.** The module only ever calls ``iterdir``/``stat``-class
  predicates. It never creates, modifies, re-permissions or removes anything,
  and it does not follow symlinked buckets (a compat symlink must not turn an
  audit into a walk of another tree).
* **Never raises for a hostile tree.** A missing, non-directory or unreadable
  root is reported in ``status``; an unreadable bucket is reported on its entry.
* Every root resolves through :mod:`mac.mac_paths` — the one sanctioned
  resolver (``tests/test_mac_paths_no_hardcode.py`` enforces it). Callers may
  pass an explicit root instead, which is what the tests do.

Duplicate-datum and orphan detection are a sibling task; ``duplicates`` and
``orphans`` are present and empty so the schema is stable for its arrival.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from mac import mac_paths

# NOTE: deliberately no ``__all__``. scripts/generate-env-config-registry.py
# records every ``MAC_*`` name that appears in a STRING constant under src/mac,
# so listing the schema constant's name as a string here would publish
# "MAC_HOME_AUDIT_SCHEMA" in the generated operator reference as though it were
# an environment variable. The public surface is the schema constant, the spec
# tables and audit_mac_home(); everything private is underscore-prefixed.

MAC_HOME_AUDIT_SCHEMA = "mac.mac_home_audit.v1"

CLASSIFICATION_CANONICAL = "canonical"
CLASSIFICATION_LEGACY = "legacy_accepted"
CLASSIFICATION_DRIFT = "drift"

GENERATION_UNIFIED = "unified"
GENERATION_PRE_PHASE2 = "pre_phase2"
GENERATION_MIXED = "mixed"
GENERATION_UNKNOWN = "unknown"

STATUS_OK = "ok"
STATUS_MISSING = "missing"
STATUS_NOT_A_DIRECTORY = "not_a_directory"
STATUS_UNREADABLE = "unreadable"


@dataclass(frozen=True)
class ExpectedPath:
    """One path §4 places inside a bucket."""

    name: str
    kind: str  # "file" | "dir"
    purpose: str


@dataclass(frozen=True)
class Bucket:
    """One canonical top-level bucket of the unified root."""

    name: str
    purpose: str
    expected: tuple[ExpectedPath, ...]
    # Recognised inside the bucket but not required to exist: the gateway's own
    # files, which only a host that actually runs the gateway has.
    also_known: tuple[str, ...] = ()

    def known_names(self) -> frozenset[str]:
        return frozenset({item.name for item in self.expected} | set(self.also_known))


@dataclass(frozen=True)
class LegacyEntry:
    """A recognised pre-migration entry sitting directly at the root.

    ``canonical_target`` is the §4 destination, or ``None`` when the plan does
    not yet name one for this entry.
    """

    name: str
    canonical_target: str | None
    note: str


# --- The approved target layout (docs/home-consolidation.md §4) -------------

CANONICAL_BUCKETS: tuple[Bucket, ...] = (
    Bucket(
        name="ledger",
        purpose="Control-plane ledger and its backups.",
        expected=(
            ExpectedPath("mac.db", "file", "Hub ledger database."),
            ExpectedPath("backups", "dir", "Dated ledger/deploy backups."),
            ExpectedPath("archive", "dir", "Ledger archive."),
        ),
    ),
    Bucket(
        name="secrets",
        purpose="The single secret source (0700).",
        expected=(
            ExpectedPath("mac.env", "file", "Hub/service secrets."),
            ExpectedPath(".env", "file", "Client deploy env (scoped fleet tokens)."),
            ExpectedPath("client-principals.json", "file", "Client principal registry."),
        ),
    ),
    Bucket(
        name="fleet",
        purpose="Fleet registry and specs.",
        expected=(
            ExpectedPath("fleets.yaml", "file", "Fleet registry."),
            ExpectedPath("specs", "dir", "Fleet specs."),
        ),
    ),
    Bucket(
        name="runtime",
        purpose="Control-plane runtime artefacts.",
        expected=(
            ExpectedPath("mac-runtime-context.json", "file", "Runtime context (machine)."),
            ExpectedPath("mac-runtime-context.md", "file", "Runtime context (operator)."),
            ExpectedPath("mac-memory-topology.json", "file", "Memory topology."),
            ExpectedPath("journal", "dir", "Dated soul/memory snapshots."),
        ),
    ),
    Bucket(
        name="gateway",
        purpose="Agent-personal gateway home (0700).",
        expected=(ExpectedPath("openclaw", "dir", "OpenClaw gateway home."),),
        also_known=(
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
    ),
    Bucket(
        name="toolchain",
        purpose="Installed source, virtualenv and executables.",
        expected=(
            ExpectedPath("src", "dir", "Installed MAC source."),
            ExpectedPath("venv", "dir", "Runtime virtualenv."),
            ExpectedPath("bin", "dir", "Installed executables."),
            ExpectedPath("hermes-agent", "dir", "Gateway agent runtime."),
        ),
    ),
)

BUCKETS_BY_NAME: dict[str, Bucket] = {bucket.name: bucket for bucket in CANONICAL_BUCKETS}


# --- The shape that is actually on disk today (pre-Phase-2) -----------------
#
# Every name below is a real root child named by first-party code (the
# mac_paths resolvers, deploy/, scripts/). Entries whose §4 destination the
# plan states carry it; the rest carry None rather than a guess.

LEGACY_TOP_LEVEL: tuple[LegacyEntry, ...] = (
    LegacyEntry("mac.db", "ledger/mac.db", "Ledger still at the root."),
    LegacyEntry("backups", "ledger/backups", "Backups still at the root."),
    LegacyEntry("archive", "ledger/archive", "Archive still at the root."),
    LegacyEntry("mac.env", "secrets/mac.env", "Hub secrets still at the root."),
    LegacyEntry(".env", "secrets/.env", "Client deploy env still at the root."),
    LegacyEntry(
        "client-principals.json",
        "secrets/client-principals.json",
        "Client principals still at the root.",
    ),
    LegacyEntry("fleets.yaml", "fleet/fleets.yaml", "Fleet registry still at the root."),
    LegacyEntry("specs", "fleet/specs", "Fleet specs still at the root."),
    LegacyEntry(
        "mac-runtime-context.json",
        "runtime/mac-runtime-context.json",
        "Runtime context still at the root.",
    ),
    LegacyEntry(
        "mac-runtime-context.md",
        "runtime/mac-runtime-context.md",
        "Runtime context still at the root.",
    ),
    LegacyEntry(
        "mac-memory-topology.json",
        "runtime/mac-memory-topology.json",
        "Memory topology still at the root.",
    ),
    LegacyEntry("journal", "runtime/journal", "Journal still at the root."),
    LegacyEntry("openclaw", "gateway/openclaw", "Gateway home still at the root."),
    LegacyEntry("src", "toolchain/src", "Installed source still at the root."),
    LegacyEntry("venv", "toolchain/venv", "Virtualenv still at the root."),
    LegacyEntry("bin", "toolchain/bin", "Executables still at the root."),
    LegacyEntry("hermes-agent", "toolchain/hermes-agent", "Agent runtime still at the root."),
    # Recognised, but §4 does not (yet) place these in a bucket.
    LegacyEntry("qdrant", None, "L2 memory store; §4 assigns it no bucket."),
    LegacyEntry("openshell", None, "OpenShell state; §4 assigns it no bucket."),
    LegacyEntry("openshell-policy.yaml", None, "OpenShell policy; §4 assigns it no bucket."),
    LegacyEntry(
        "openshell-gateway-policy.yaml",
        None,
        "OpenShell gateway policy; §4 assigns it no bucket.",
    ),
    LegacyEntry("agent-workspaces", None, "Worker workspaces; §4 assigns them no bucket."),
    LegacyEntry("public-artifacts", None, "Published artefacts; §4 assigns them no bucket."),
    LegacyEntry("clients", None, "Client state; §4 assigns it no bucket."),
    LegacyEntry("credentials", None, "Credential material; §4 assigns it no bucket."),
    LegacyEntry("keys", None, "Key material; §4 assigns it no bucket."),
    LegacyEntry("ssh", None, "SSH material; §4 assigns it no bucket."),
    LegacyEntry("sessions", None, "Session state; §4 assigns it no bucket."),
    LegacyEntry("logs", None, "Logs; §4 assigns them no bucket."),
    LegacyEntry("fleet-finalizers", None, "Fleet finalizers; §4 assigns them no bucket."),
)

LEGACY_BY_NAME: dict[str, LegacyEntry] = {entry.name: entry for entry in LEGACY_TOP_LEVEL}


# --- Read-only probes -------------------------------------------------------
#
# The complete set of filesystem calls this module makes. Each answers a
# question and swallows the OSError a hostile tree can raise, so an audit of a
# broken root degrades into a report instead of a traceback.


def _kind(path: Path) -> str:
    """Describe an entry without following it."""
    try:
        if path.is_symlink():
            return "symlink"
        if path.is_dir():
            return "dir"
        if path.is_file():
            return "file"
    except OSError:
        return "unknown"
    return "other"


def _children(path: Path) -> tuple[list[str], str | None]:
    """Names directly under ``path``; ``(names, problem)`` never raises."""
    try:
        return sorted(child.name for child in path.iterdir()), None
    except OSError as error:
        return [], error.strerror or error.__class__.__name__


def _exists(path: Path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _is_dir(path: Path) -> bool:
    try:
        return path.is_dir()
    except OSError:
        return False


def _root_status(root: Path) -> tuple[str, str | None, list[str]]:
    """``(state, detail, top-level names)`` for a root of any shape."""
    if not _exists(root):
        return STATUS_MISSING, "root does not exist", []
    # The root itself is followed if it is a symlink -- the caller named it, and
    # the transition plan leaves compat symlinks pointing at real roots. Buckets
    # inside it are NOT followed (see _classify_bucket_children).
    if not _is_dir(root):
        return STATUS_NOT_A_DIRECTORY, "root is not a directory", []
    names, problem = _children(root)
    if problem is not None:
        return STATUS_UNREADABLE, problem, []
    return STATUS_OK, None, names


# --- Classification ---------------------------------------------------------


def _entry(
    *,
    name: str,
    path: str,
    kind: str,
    classification: str,
    generation: str,
    bucket: str | None = None,
    canonical_target: str | None = None,
    detail: str | None = None,
) -> dict[str, object]:
    return {
        "name": name,
        "path": path,
        "kind": kind,
        "classification": classification,
        "generation": generation,
        "bucket": bucket,
        "canonical_target": canonical_target,
        "detail": detail,
    }


def _classify_top_level(name: str, path: Path) -> dict[str, object]:
    kind = _kind(path)
    bucket = BUCKETS_BY_NAME.get(name)
    if bucket is not None:
        return _entry(
            name=name,
            path=name,
            kind=kind,
            classification=CLASSIFICATION_CANONICAL,
            generation=GENERATION_UNIFIED,
            bucket=name,
            detail=bucket.purpose,
        )
    legacy = LEGACY_BY_NAME.get(name)
    if legacy is not None:
        target = legacy.canonical_target
        return _entry(
            name=name,
            path=name,
            kind=kind,
            classification=CLASSIFICATION_LEGACY,
            generation=GENERATION_PRE_PHASE2,
            bucket=target.split("/", 1)[0] if target else None,
            canonical_target=target,
            detail=legacy.note,
        )
    return _entry(
        name=name,
        path=name,
        kind=kind,
        classification=CLASSIFICATION_DRIFT,
        generation=GENERATION_UNKNOWN,
        detail="not a §4 bucket and not a recognised pre-migration location",
    )


def _classify_bucket_children(
    bucket: Bucket, bucket_path: Path
) -> tuple[list[dict[str, object]], str | None]:
    """One level inside a canonical bucket: ``(entries, problem)``.

    ``problem`` is a note for the bucket's own entry when the bucket could not
    be listed -- either because it is unreadable, or because it is not a real
    directory. A symlinked bucket is deliberately NOT followed: during the
    transition a bucket may be a compat symlink, and an auditor that walked it
    would report another tree's contents as this root's.
    """
    kind = _kind(bucket_path)
    if kind != "dir":
        return [], "bucket not descended (kind=%s)" % kind
    names, problem = _children(bucket_path)
    if problem is not None:
        return [], "bucket is unreadable: %s" % problem
    known = bucket.known_names()
    entries: list[dict[str, object]] = []
    for name in names:
        relative = "%s/%s" % (bucket.name, name)
        entries.append(
            _entry(
                name=name,
                path=relative,
                kind=_kind(bucket_path / name),
                classification=(
                    CLASSIFICATION_CANONICAL if name in known else CLASSIFICATION_DRIFT
                ),
                generation=GENERATION_UNIFIED if name in known else GENERATION_UNKNOWN,
                bucket=bucket.name,
                detail=None if name in known else "not named by §4 for this bucket",
            )
        )
    return entries, None


def _missing_expected(root: Path, present_top_level: set[str]) -> list[dict[str, object]]:
    """Canonical paths that are absent, and the legacy location that covers each."""
    missing: list[dict[str, object]] = []
    for bucket in CANONICAL_BUCKETS:
        for expected in bucket.expected:
            relative = "%s/%s" % (bucket.name, expected.name)
            if _exists(root / bucket.name / expected.name):
                continue
            legacy_name = next(
                (
                    entry.name
                    for entry in LEGACY_TOP_LEVEL
                    if entry.canonical_target == relative and entry.name in present_top_level
                ),
                None,
            )
            missing.append(
                {
                    "path": relative,
                    "bucket": bucket.name,
                    "kind": expected.kind,
                    "purpose": expected.purpose,
                    "satisfied_by_legacy": legacy_name,
                }
            )
    return missing


def _generation(entries: list[dict[str, object]]) -> str:
    classifications = {entry["classification"] for entry in entries}
    canonical = CLASSIFICATION_CANONICAL in classifications
    legacy = CLASSIFICATION_LEGACY in classifications
    if canonical and legacy:
        return GENERATION_MIXED
    if canonical:
        return GENERATION_UNIFIED
    if legacy:
        return GENERATION_PRE_PHASE2
    return GENERATION_UNKNOWN


def _resolvers(root: Path) -> dict[str, object]:
    """What the sanctioned resolver currently points at, relative to this audit."""
    gateway = mac_paths.gateway_home()
    openclaw = mac_paths.openclaw_home()
    return {
        "mac_home": str(mac_paths.mac_home()),
        "gateway_home": str(gateway),
        "openclaw_home": str(openclaw),
        "gateway_home_inside_root": _is_inside(gateway, root),
        "openclaw_home_inside_root": _is_inside(openclaw, root),
    }


def _is_inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def audit_mac_home(
    root: str | Path | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    """Audit the unified MAC home root and return a report dict.

    :param root: the root to audit; defaults to :func:`mac.mac_paths.mac_home`.
    :param now: audit timestamp, for reproducible reports (defaults to UTC now).
    :returns: a ``mac.mac_home_audit.v1`` document. Nothing on disk is modified,
        and a missing or unreadable root produces a report, never an exception.
    """
    resolved = Path(root).expanduser() if root is not None else mac_paths.mac_home()
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    state, detail, top_level_names = _root_status(resolved)

    top_level = {name: _classify_top_level(name, resolved / name) for name in top_level_names}
    nested: list[dict[str, object]] = []
    for bucket in CANONICAL_BUCKETS:
        if bucket.name not in top_level:
            continue
        children, problem = _classify_bucket_children(bucket, resolved / bucket.name)
        nested.extend(children)
        if problem is not None:
            top_level[bucket.name]["detail"] = problem
    entries = [*top_level.values(), *nested]

    present = set(top_level_names)
    missing = _missing_expected(resolved, present) if state == STATUS_OK else []
    by_class = {
        name: [entry["path"] for entry in entries if entry["classification"] == name]
        for name in (CLASSIFICATION_CANONICAL, CLASSIFICATION_LEGACY, CLASSIFICATION_DRIFT)
    }
    unmapped_legacy = [
        entry["path"]
        for entry in entries
        if entry["classification"] == CLASSIFICATION_LEGACY and entry["canonical_target"] is None
    ]

    return {
        "schema": MAC_HOME_AUDIT_SCHEMA,
        "root_path": str(resolved),
        "audited_at": stamp.isoformat(),
        "root_exists": state != STATUS_MISSING,
        "status": {"state": state, "detail": detail},
        "resolvers": _resolvers(resolved),
        "layout": {
            "generation_detected": _generation(entries),
            "buckets": [bucket.name for bucket in CANONICAL_BUCKETS],
            "buckets_present": [
                bucket.name for bucket in CANONICAL_BUCKETS if bucket.name in present
            ],
        },
        "entries": entries,
        "canonical": by_class[CLASSIFICATION_CANONICAL],
        "legacy_accepted": by_class[CLASSIFICATION_LEGACY],
        "drift": by_class[CLASSIFICATION_DRIFT],
        "legacy_without_canonical_target": unmapped_legacy,
        "missing_expected": missing,
        # Reserved for the sibling duplicate/orphan detector; the keys exist now
        # so consumers of v1 do not have to change shape when it lands.
        "duplicates": [],
        "orphans": [],
        "summary": {
            "entries": len(entries),
            "canonical": len(by_class[CLASSIFICATION_CANONICAL]),
            "legacy_accepted": len(by_class[CLASSIFICATION_LEGACY]),
            "legacy_without_canonical_target": len(unmapped_legacy),
            "drift": len(by_class[CLASSIFICATION_DRIFT]),
            "missing_expected": len(missing),
            "buckets_present": len([b for b in CANONICAL_BUCKETS if b.name in present]),
            "duplicates": 0,
            "orphans": 0,
        },
    }
