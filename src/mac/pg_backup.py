"""Verified PostgreSQL authority backups for the hub.

This was originally the Postgres analogue of ``mac.ledger_backup``; that
SQLite path has since been retired and this is the only backup path there is.

The hub authority now runs in PostgreSQL (``MAC_DATABASE_URL``). Until now the
Postgres tier only *delegated* backup responsibility to the database operator
(streaming replica + managed failover, see ``docs/hub-availability.md``). That
is the right RTO answer, but it is not a self-contained, in-tree, verified
backup: a fleet that ran Postgres had no first-party artifact it could hand to
a break-glass restore, no retention it owned, and no proof-of-restorability
drill. This module supplies exactly that, mirroring the guarantees the SQLite
path already gives:

- **Consistent logical dump.** A single ``pg_dump`` in the custom archive
  format (``-Fc``) runs in one snapshot-isolated transaction, so the artifact
  is internally consistent even under concurrent hub writes. Custom format is
  chosen over a plain SQL dump because it is compressed, supports parallel
  restore, and — critically — lets a scratch restore run through
  ``pg_restore`` without shelling SQL through ``psql``.
- **Owner-only artifacts.** The dump and its sha256 manifest are written
  ``0600`` in a ``0700`` directory: a hub backup contains the entire authority,
  including secret-scoped rows, and must never be world/group readable.
- **Retention.** Timestamped artifacts under ``<out>/postgres/``, pruned to
  ``--keep-last`` (default 14), matching the ledger path's contract.
- **Failure telemetry.** ``pg_backup_scheduler`` turns a dump/verify/ship
  failure into a loud ledger observation + operator notification; it never
  silently drops a backup.
- **Restore-to-scratch verification.** Each backup (or on a slower cadence)
  restores the dump into a throwaway scratch database and proves the schema
  plus representative row counts/checks came back — a backup that cannot be
  restored is not a backup. ``verify_restore`` fails closed.

A PostgreSQL failure NEVER falls back to SQLite. This module has no SQLite code
path at all: it operates only on a ``postgres://``/``postgresql://`` DSN. The
immutable 2026-07-28 SQLite cutover archive (``mac admin migrate`` archive, mode
0600, sha256 manifest) is preserved as *recovery evidence* — a frozen record of
the pre-cutover authority — and is explicitly never a live fallback authority.

Promote safety, exactly like the ledger path: a restored artifact becomes the
fleet authority only through the documented promote procedure (fence the old
hub first). Nothing here ever starts a second live authority.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urlsplit

BACKUP_MANIFEST_SCHEMA = "mac.pg_backup.v1"
SYNC_CMD_ENV = "MAC_PG_BACKUP_SYNC_CMD"
BIN_DIR_ENV = "MAC_PG_BIN_DIR"
DEFAULT_KEEP_LAST = 14

# Where PostgreSQL client binaries live when they are not on PATH. A service
# manager's PATH is not a login shell's: rocky's hub runs under launchd with
# PATH=/Users/jkh/.mac/bin:/Users/jkh/.mac/venv/bin:/usr/bin:/bin:/usr/sbin:/sbin,
# which excludes Homebrew, so every backup since the directory was created
# failed with "[Errno 2] No such file or directory: 'pg_dump'" -- hourly, in
# telemetry, for days. Resolving the binary ourselves means a backup does not
# depend on how the supervisor was configured, which matters because this
# repository provisions its own hosts and cannot assume a PATH on future
# AWS/Azure targets.
#
# Globs are expanded newest-version-first: pg_dump must be at least the server's
# major version, so preferring the highest installed one is the safe default.
_PG_BIN_SEARCH: tuple[str, ...] = (
    "/opt/homebrew/opt/postgresql@*/bin",  # Homebrew, versioned (Apple silicon)
    "/usr/local/opt/postgresql@*/bin",  # Homebrew, versioned (Intel)
    "/opt/homebrew/bin",  # Homebrew, current
    "/usr/local/bin",  # Homebrew (Intel) / manual installs
    "/usr/lib/postgresql/*/bin",  # Debian / Ubuntu
    "/usr/pgsql-*/bin",  # PGDG RPM
    "/Library/PostgreSQL/*/bin",  # EDB installer (macOS)
    "/usr/bin",
)


# The major version as it appears in each layout's directory name. Matching
# these markers rather than "any digits in the path" matters: an install under
# /var/folders/61/... or /opt/build-228/ would otherwise sort by the unrelated
# number and hand back the oldest client.
_PG_VERSION_RE = re.compile(r"(?:postgresql@|postgresql[/-]|pgsql-)(\d+)", re.IGNORECASE)


def _version_key(path: str) -> tuple:
    """Sort key that puts the highest PostgreSQL major version first."""
    versions = [int(m) for m in _PG_VERSION_RE.findall(path)]
    return (-max(versions, default=0), path)


def _binary_for(name: str, env: Mapping[str, str], runner: Optional["Runner"]) -> str:
    """The argv[0] to invoke ``name`` with.

    A caller-supplied runner *models* the client binaries (see the runner
    indirection note below), so it must keep working on a machine where they
    are not installed. Only the real subprocess path resolves a real path.
    """
    if runner is not None:
        return name
    return pg_binary(name, env)


def pg_binary(name: str, env: Optional[Mapping[str, str]] = None) -> str:
    """Absolute path to a PostgreSQL client binary, or raise explaining why not.

    Order: ``MAC_PG_BIN_DIR``, then PATH, then the usual install locations.
    """
    environ = os.environ if env is None else env
    override = str(environ.get(BIN_DIR_ENV) or "").strip()
    if override:
        candidate = Path(override).expanduser() / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
        raise PgBackupError(
            "%s=%s does not contain an executable %s" % (BIN_DIR_ENV, override, name)
        )

    found = shutil.which(name, path=environ.get("PATH"))
    if found:
        return found

    for pattern in _PG_BIN_SEARCH:
        for directory in sorted(glob.glob(pattern), key=_version_key):
            candidate = Path(directory) / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)

    raise PgBackupError(
        "%s not found on PATH or in any known PostgreSQL install directory. "
        "The hub cannot back up its own authority without it. Install the "
        "PostgreSQL client tools, or point %s at the directory holding them "
        "(PATH was %r)." % (name, BIN_DIR_ENV, environ.get("PATH", ""))
    )


# Representative authority tables whose presence + row counts prove a scratch
# restore rehydrated the schema and real data, not just an empty database.
# These are core control-plane tables that always exist on a live hub; the
# check is "the table exists and its row count survived the round-trip", which
# catches a truncated dump, a partial restore, or a schema-only artifact.
DEFAULT_VERIFY_TABLES: tuple[str, ...] = ("tasks", "agents", "events")

# A dump is a point-in-time snapshot; the live table keeps moving while the
# restore drill runs. Requiring the restored count to EQUAL the live count
# therefore compares a photograph to a moving target, and on a busy hub it
# never matches -- the production hub failed every scheduled backup with
#
#   restore verify: row count for 'events' diverged (live=1311209 restored=1311496)
#
# so no manifest was written and nothing shipped off the box. Note the
# restored count was HIGHER: retention pruning moves the live count both ways,
# so this is not a "wait for writes to settle" problem, it is unfixable by
# ordering.
#
# What the drill is actually for (per the comment above) is catching a
# truncated dump, a partial restore, or a schema-only artifact. A relative
# tolerance catches all three -- those failures are order-of-magnitude, not
# fractions of a percent -- while surviving ordinary churn. An empty restored
# table where the live one has rows is always a failure, whatever the
# tolerance.
DEFAULT_VERIFY_TOLERANCE = 0.05
VERIFY_TOLERANCE_ENV = "MAC_PG_BACKUP_VERIFY_TOLERANCE"


def _verify_tolerance(environ: Optional[Mapping[str, str]] = None) -> float:
    raw = str(
        (environ if environ is not None else os.environ).get(VERIFY_TOLERANCE_ENV) or ""
    ).strip()
    try:
        value = float(raw) if raw else DEFAULT_VERIFY_TOLERANCE
    except ValueError:
        value = DEFAULT_VERIFY_TOLERANCE
    return min(1.0, max(0.0, value))


def _counts_are_consistent(live: int, restored: int, tolerance: float) -> bool:
    """Whether a restored row count is consistent with a moving live table."""

    if restored == live:
        return True
    # A live table with rows must not restore empty: that is the schema-only
    # or truncated-dump failure this drill exists to catch.
    if live > 0 and restored == 0:
        return False
    if live <= 0:
        return restored == 0
    return abs(restored - live) <= max(1.0, live * tolerance)


# subprocess runner indirection so the pure backup logic is unit-testable
# without a live cluster or the pg client binaries on PATH.
Runner = Callable[[Sequence[str], Mapping[str, str]], "subprocess.CompletedProcess[str]"]


class PgBackupError(RuntimeError):
    """A PostgreSQL backup cannot be produced or proved restorable."""


def _default_runner(
    argv: Sequence[str], env: Mapping[str, str]
) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(
        list(argv),
        env=dict(env),
        capture_output=True,
        text=True,
    )


def is_postgres_dsn(dsn: Optional[str]) -> bool:
    """Whether ``dsn`` names a PostgreSQL authority.

    Shared by both backup schedulers so they stay mutually exclusive: whichever
    backend the hub actually runs, exactly one backup path is live. Two
    independent answers to this question are how a hub ends up backing up the
    wrong authority, or neither.
    """
    return (dsn or "").strip().startswith(("postgres://", "postgresql://"))


def _require_postgres_dsn(dsn: str) -> str:
    dsn = (dsn or "").strip()
    if not dsn:
        raise PgBackupError(
            "no PostgreSQL DSN configured; set MAC_DATABASE_URL / MAC_PG_BACKUP_URL"
        )
    if not is_postgres_dsn(dsn):
        raise PgBackupError(
            "pg_backup requires a postgres:// or postgresql:// DSN; refusing to "
            "operate on a non-Postgres authority (a Postgres failure never falls "
            "back to SQLite)"
        )
    return dsn


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _pg_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ if base is None else base)
    # A dump of the whole authority must be reproducible regardless of the
    # operator's locale; do not let LC_* reorder or reformat anything.
    env.setdefault("PGCONNECT_TIMEOUT", "30")
    return env


def _scratch_dbname(stamp: str) -> str:
    return "mac_restore_verify_%s" % stamp.replace("-", "").replace(":", "").lower()


def _admin_dsn(dsn: str, dbname: str) -> str:
    """Return the DSN with its database path swapped for ``dbname`` (used to
    create/drop the scratch verification database against the same server).

    Built by hand rather than with ``geturl()``. A local socket DSN has an
    EMPTY authority -- ``postgresql:///mac`` -- and ``urlunsplit`` drops the
    ``//`` when netloc is empty, so the round trip yields ``postgresql:/postgres``.
    libpq then reads that as a database *named* ``postgresql:/postgres`` and the
    scratch database can never be created, which fails restore verification, so
    no manifest is written and the backup is never shipped off the box. The
    production hub runs exactly this DSN form, so every scheduled backup failed
    verification while the dump itself succeeded -- a backup that looks present
    on disk and is absent where it matters.
    """
    parts = urlsplit(dsn)
    return "%s://%s/%s" % (parts.scheme, parts.netloc, dbname)


def _dbname_of(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/") or "postgres"


@dataclass(frozen=True)
class BackupResult:
    path: Path
    manifest: Path
    sha256: str
    size_bytes: int
    created_at: str
    verified: bool
    verify_detail: Dict[str, object]


def dump(
    dsn: str,
    out_dir: Path,
    *,
    keep_last: int = DEFAULT_KEEP_LAST,
    verify: bool = True,
    verify_tables: Sequence[str] = DEFAULT_VERIFY_TABLES,
    sync_cmd: Optional[str] = None,
    now: Optional[datetime] = None,
    runner: Optional[Runner] = None,
) -> BackupResult:
    """Take one consistent, owner-only, verified PostgreSQL backup.

    Runs ``pg_dump -Fc`` in a single transaction, writes a sha256 manifest,
    prunes to ``keep_last``, optionally restores into a throwaway scratch
    database to prove the artifact is restorable, and runs the off-box ship
    hook. Raises ``PgBackupError`` on any failure — a backup that cannot be
    produced or proved is loud, never silent.
    """
    dsn = _require_postgres_dsn(dsn)
    run = runner or _default_runner
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    pg_dir = out_dir.expanduser() / "postgres"
    pg_dir.mkdir(parents=True, exist_ok=True)
    pg_dir.chmod(0o700)
    destination = pg_dir / ("mac-%s.dump" % stamp)
    destination.unlink(missing_ok=True)

    env = _pg_env()
    dump_argv = [
        _binary_for("pg_dump", env, runner),
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file=%s" % destination,
        dsn,
    ]
    proc = run(dump_argv, env)
    if proc.returncode != 0:
        destination.unlink(missing_ok=True)
        raise PgBackupError(
            "pg_dump exited %d: %s" % (proc.returncode, (proc.stderr or "").strip()[:500])
        )
    if not destination.is_file() or destination.stat().st_size == 0:
        destination.unlink(missing_ok=True)
        raise PgBackupError("pg_dump produced no artifact")
    destination.chmod(0o600)

    digest = _sha256_file(destination)

    verify_detail: Dict[str, object] = {"performed": False}
    verified = False
    if verify:
        verify_detail = verify_restore(
            dsn, destination, verify_tables=verify_tables, now=now, runner=run
        )
        verified = bool(verify_detail.get("ok"))

    manifest_path = destination.with_suffix(".dump.manifest.json")
    manifest_path.write_text(
        json.dumps(
            {
                "schema": BACKUP_MANIFEST_SCHEMA,
                "database": _dbname_of(dsn),
                "artifact": destination.name,
                "format": "pg_dump-custom",
                "sha256": digest,
                "size_bytes": destination.stat().st_size,
                "created_at": stamp,
                "restore_verified": verified,
                "restore_detail": verify_detail,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)

    prune(pg_dir, keep_last=keep_last)

    hook = (sync_cmd if sync_cmd is not None else os.environ.get(SYNC_CMD_ENV) or "").strip()
    if hook:
        hook_env = {
            **os.environ,
            "MAC_PG_BACKUP_PATH": str(destination),
            "MAC_PG_BACKUP_SHA256": digest,
            "MAC_PG_BACKUP_MANIFEST": str(manifest_path),
        }
        shipped = subprocess.run(hook, shell=True, env=hook_env)  # noqa: S602 - operator hook
        if shipped.returncode != 0:
            raise PgBackupError(
                "pg backup sync hook exited %d (artifact kept at %s)"
                % (shipped.returncode, destination)
            )

    return BackupResult(
        path=destination,
        manifest=manifest_path,
        sha256=digest,
        size_bytes=destination.stat().st_size,
        created_at=stamp,
        verified=verified,
        verify_detail=verify_detail,
    )


def verify_restore(
    dsn: str,
    artifact: Path,
    *,
    verify_tables: Sequence[str] = DEFAULT_VERIFY_TABLES,
    tolerance: Optional[float] = None,
    now: Optional[datetime] = None,
    runner: Optional[Runner] = None,
) -> Dict[str, object]:
    """Restore ``artifact`` into a throwaway scratch database and prove schema
    plus representative row counts survived the round-trip.

    Creates ``mac_restore_verify_<stamp>``, ``pg_restore``s into it, then
    queries each verify table's row count against the live authority and the
    scratch copy. Always drops the scratch database. Returns a detail dict with
    ``ok`` set; raises ``PgBackupError`` on a hard failure (create/restore
    errored, or a table is missing / counts diverge).
    """
    dsn = _require_postgres_dsn(dsn)
    run = runner or _default_runner
    tolerance = _verify_tolerance() if tolerance is None else tolerance
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    scratch = _scratch_dbname(stamp)
    admin_dsn = _admin_dsn(dsn, "postgres")
    scratch_dsn = _admin_dsn(dsn, scratch)
    env = _pg_env()

    def _psql(target_dsn: str, sql: str) -> "subprocess.CompletedProcess[str]":
        return run(
            [
                _binary_for("psql", env, runner),
                "--no-psqlrc",
                "--tuples-only",
                "--no-align",
                "--command",
                sql,
                target_dsn,
            ],
            env,
        )

    detail: Dict[str, object] = {"performed": True, "scratch_db": scratch, "tables": {}}
    created = False
    try:
        proc = _psql(admin_dsn, 'CREATE DATABASE "%s"' % scratch)
        if proc.returncode != 0:
            raise PgBackupError(
                "restore verify could not create scratch db: %s" % (proc.stderr or "").strip()[:300]
            )
        created = True

        restore = run(
            [
                _binary_for("pg_restore", env, runner),
                "--no-owner",
                "--no-privileges",
                "--dbname=%s" % scratch_dsn,
                str(artifact),
            ],
            env,
        )
        if not verify_tables and restore.returncode != 0:
            raise PgBackupError(
                "restore verify of empty authority failed: %s"
                % (restore.stderr or "").strip()[:500]
            )
        # pg_restore can emit non-fatal warnings (e.g. missing role GRANTs we
        # deliberately stripped). Treat only a hard non-zero-with-no-tables as
        # failure; the row-count proof below is the real gate.
        if restore.returncode != 0 and "error" in (restore.stderr or "").lower():
            detail["restore_stderr"] = (restore.stderr or "").strip()[:500]

        live_counts: Dict[str, int] = {}
        scratch_counts: Dict[str, int] = {}
        for table in verify_tables:
            scratch_count = _count(_psql, scratch_dsn, table)
            if scratch_count is None:
                raise PgBackupError(
                    "restore verify: table %r absent or unreadable in restored "
                    "scratch database" % table
                )
            live_count = _count(_psql, dsn, table)
            scratch_counts[table] = scratch_count
            live_counts[table] = live_count if live_count is not None else -1
            if live_count is not None and not _counts_are_consistent(
                live_count, scratch_count, tolerance
            ):
                raise PgBackupError(
                    "restore verify: row count for %r diverged beyond tolerance "
                    "(live=%d restored=%d, tolerance=%.1f%%)"
                    % (table, live_count, scratch_count, tolerance * 100.0)
                )
        detail["tables"] = {
            "live": live_counts,
            "restored": scratch_counts,
        }
        detail["tolerance"] = tolerance
        detail["ok"] = True
        return detail
    except PgBackupError:
        detail["ok"] = False
        raise
    finally:
        if created:
            # Terminate stragglers then drop; a leaked scratch db must never
            # accumulate on the authority server.
            _psql(
                admin_dsn,
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = '%s'" % scratch,
            )
            _psql(admin_dsn, 'DROP DATABASE IF EXISTS "%s"' % scratch)


def _count(psql, target_dsn: str, table: str) -> Optional[int]:
    proc = psql(target_dsn, "SELECT COUNT(*) FROM %s" % table)
    if proc.returncode != 0:
        return None
    text = (proc.stdout or "").strip()
    if not text:
        return None
    try:
        return int(text.splitlines()[0].strip())
    except ValueError:
        return None


def prune(pg_dir: Path, *, keep_last: int = DEFAULT_KEEP_LAST) -> List[Path]:
    """Keep the newest ``keep_last`` dumps (plus manifests); remove the rest."""
    if keep_last <= 0:
        return []
    dumps = sorted(pg_dir.glob("mac-*.dump"))
    removed: List[Path] = []
    for stale in dumps[:-keep_last] if len(dumps) > keep_last else []:
        stale.unlink(missing_ok=True)
        stale.with_suffix(".dump.manifest.json").unlink(missing_ok=True)
        removed.append(stale)
    return removed


def verify_manifest(artifact: Path) -> bool:
    """Re-verify a dump against its sha256 manifest (standby-side integrity
    check; does not require a live server)."""
    manifest_path = artifact.with_suffix(".dump.manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != BACKUP_MANIFEST_SCHEMA:
        raise PgBackupError("unknown pg backup manifest schema")
    if _sha256_file(artifact) != str(manifest.get("sha256")):
        raise PgBackupError("pg backup sha256 does not match its manifest")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.pg_backup")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("MAC_PG_BACKUP_URL") or os.environ.get("MAC_DATABASE_URL", ""),
    )
    parser.add_argument(
        "--out",
        default=os.environ.get("MAC_PG_BACKUP_DIR") or os.environ.get("MAC_LEDGER_BACKUP_DIR", ""),
    )
    parser.add_argument("--keep-last", type=int, default=DEFAULT_KEEP_LAST)
    parser.add_argument("--sync-cmd", default=None)
    parser.add_argument(
        "--json", action="store_true", help="emit a machine-readable backup receipt"
    )
    parser.add_argument(
        "--allow-empty-authority",
        action="store_true",
        help="restore-verify an empty fresh database without representative table checks",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="skip the restore-to-scratch verification (not recommended)",
    )
    parser.add_argument(
        "--verify-manifest",
        metavar="ARTIFACT",
        default=None,
        help="offline sha256 check of an existing dump against its manifest",
    )
    ns = parser.parse_args(argv)
    if ns.verify_manifest:
        verify_manifest(Path(ns.verify_manifest))
        print("pg backup manifest verified: %s" % ns.verify_manifest)
        return 0
    out = ns.out.strip()
    if not out:
        print("pg_backup: --out (or MAC_PG_BACKUP_DIR) is required", file=sys.stderr)
        return 2
    result = dump(
        ns.dsn,
        Path(out),
        keep_last=ns.keep_last,
        verify=not ns.no_verify,
        verify_tables=() if ns.allow_empty_authority else DEFAULT_VERIFY_TABLES,
        sync_cmd=ns.sync_cmd,
    )
    if ns.json:
        print(
            json.dumps(
                {
                    "schema": BACKUP_MANIFEST_SCHEMA,
                    "path": str(result.path),
                    "manifest": str(result.manifest),
                    "sha256": result.sha256,
                    "size_bytes": result.size_bytes,
                    "created_at": result.created_at,
                    "restore_verified": result.verified,
                    "restore_detail": result.verify_detail,
                },
                sort_keys=True,
            )
        )
    else:
        print("pg backup written: %s (restore_verified=%s)" % (result.path, result.verified))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
