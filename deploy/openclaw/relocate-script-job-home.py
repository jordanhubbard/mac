#!/usr/bin/env python3
"""Untangle the Hermes/OpenClaw script-job homes on one host.

Two independent, idempotent operations, both non-destructive (nothing is ever
deleted; everything moved is verified by digest first and left recoverable):

``scripts`` (default)
    Move ``$HERMES_HOME/scripts`` -> ``$MAC_OPENCLAW_HOST_DIR/script-jobs/scripts``
    so a job's code lives in the same home as its definitions and its output.
    Each file is copied, digest-verified, then unlinked; a file already present
    and identical at the destination is skipped, and one that differs is left in
    place and reported as a conflict rather than guessed at. When the source
    directory ends up empty it is replaced by a symlink to the destination, so
    anything still holding the old path (an unreinstalled launchd plist, an
    operator's muscle memory) keeps working while the runner reads the new home.

``config-backups``
    Resolve the ``config.yaml`` backup pile in the gateway home. The live file is
    ``config.yaml`` itself — that is the only name the gateway reads, so it is
    determined, not guessed. Every ``config.yaml.<suffix>`` variant is moved to
    ``$MAC_HOME/backups/hermes-config-<UTC date>/`` alongside a
    ``WHICH-WAS-LIVE.md`` note recording which file was live, its digest, and
    every archived variant with its digest and mtime. If ``config.yaml`` itself
    is missing the command REFUSES rather than promote a backup, because at that
    point which one was authoritative is not determinable from the tree — which
    is the exact failure this whole exercise is about.

Standard library only, and safe to re-run: a second run reports
``already-relocated`` / ``already-archived`` and changes nothing.

Usage:
    relocate-script-job-home.py --dry-run              # show the plan, touch nothing
    relocate-script-job-home.py --apply
    relocate-script-job-home.py --apply config-backups
    relocate-script-job-home.py --apply all
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import time
from typing import Optional

# Suffix families observed on the live hub (2026-08-21): .bak,
# .bak-mac-home-sync, .bak-mac-shutdown-quench, .chatbak, .provbak.*,
# .mac-redaction-backup-*. Matching on the "config.yaml." prefix rather than an
# enumeration means a suffix nobody has seen yet is still swept up.
CONFIG_NAME = "config.yaml"


# --------------------------------------------------------------------------- #
# Home resolution — MIRROR of ``mac.mac_paths`` (see run-script-cron-job.py)    #
# Pinned against the real module by tests/test_script_job_home.py.              #
# --------------------------------------------------------------------------- #
def _env_dir(name: str) -> Optional[Path]:
    value = (os.environ.get(name) or "").strip()
    return Path(value).expanduser() if value else None


def mac_home() -> Path:
    return _env_dir("MAC_HOME") or (Path.home() / ".mac")


def openclaw_home() -> Path:
    return _env_dir("MAC_OPENCLAW_HOST_DIR") or (mac_home() / "openclaw")


def gateway_home() -> Path:
    return _env_dir("HERMES_HOME") or (Path.home() / ".hermes")


def script_jobs_dir() -> Path:
    return openclaw_home() / "script-jobs"


def script_jobs_scripts_dir() -> Path:
    return _env_dir("MAC_OPENCLAW_SCRIPT_JOB_SCRIPTS_DIR") or (
        script_jobs_dir() / "scripts"
    )


def legacy_gateway_scripts_dir() -> Path:
    return gateway_home() / "scripts"


def backups_dir() -> Path:
    return mac_home() / "backups"


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def digest(path: Path) -> str:
    """SHA-256 of a file, streamed so a large script does not land in memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _stamp() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _relative_symlink_ok(source: Path, destination: Path) -> bool:
    """True when ``source`` is already a symlink resolving to ``destination``."""
    if not source.is_symlink():
        return False
    try:
        return source.resolve() == destination.resolve()
    except OSError:
        return False


# --------------------------------------------------------------------------- #
# Operation: relocate the scripts home                                          #
# --------------------------------------------------------------------------- #
def relocate_scripts(
    source: Path, destination: Path, *, apply: bool = False
) -> dict:
    """Move ``source``/* to ``destination``, digest-verified and idempotent."""
    result: dict = {
        "operation": "scripts",
        "source": str(source),
        "destination": str(destination),
        "status": "ok",
        "moved": [],
        "skipped_identical": [],
        "conflicts": [],
        "symlinked": False,
        "applied": bool(apply),
    }

    if _relative_symlink_ok(source, destination):
        result["status"] = "already-relocated"
        result["symlinked"] = True
        return result
    if not source.exists():
        result["status"] = "nothing-to-do"
        return result
    if not source.is_dir():
        result["status"] = "source-not-a-directory"
        return result
    if destination.exists() and source.resolve() == destination.resolve():
        result["status"] = "already-relocated"
        return result

    if apply:
        destination.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(destination, 0o700)
        except OSError:
            pass

    for entry in sorted(source.rglob("*")):
        if entry.is_dir() or entry.is_symlink():
            continue
        relative = entry.relative_to(source)
        target = destination / relative
        if target.exists():
            if digest(target) == digest(entry):
                result["skipped_identical"].append(str(relative))
                if apply:
                    entry.unlink()
                continue
            # Two different files claiming one name is exactly the ambiguity
            # this task exists to remove; refuse to pick a winner.
            result["conflicts"].append(str(relative))
            result["status"] = "conflicts"
            continue
        if not apply:
            result["moved"].append(str(relative))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(entry, target)
        if digest(target) != digest(entry):
            target.unlink(missing_ok=True)
            result["conflicts"].append(str(relative))
            result["status"] = "verify-failed"
            continue
        entry.unlink()
        result["moved"].append(str(relative))

    if result["conflicts"]:
        return result

    # Leave a compat symlink so an un-reinstalled schedule keeps resolving.
    if not apply:
        result["symlinked"] = True  # what --apply would do
        return result
    if any(item.is_file() for item in source.rglob("*")):
        return result
    shutil.rmtree(source, ignore_errors=True)
    try:
        source.symlink_to(destination, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform dependent
        result["status"] = "symlink-failed: %s" % exc
        return result
    result["symlinked"] = True
    return result


# --------------------------------------------------------------------------- #
# Operation: resolve the config.yaml backup pile                                #
# --------------------------------------------------------------------------- #
def archive_config_backups(
    home: Path, archive_root: Path, *, apply: bool = False
) -> dict:
    """Keep ``config.yaml``; archive every ``config.yaml.*`` variant with a note."""
    live = home / CONFIG_NAME
    archive = archive_root / ("hermes-config-%s" % _stamp())
    result: dict = {
        "operation": "config-backups",
        "home": str(home),
        "live": str(live),
        "archive": str(archive),
        "status": "ok",
        "archived": [],
        "applied": bool(apply),
    }

    if not home.is_dir():
        result["status"] = "nothing-to-do"
        return result
    variants = sorted(
        path
        for path in home.iterdir()
        if path.is_file()
        and not path.is_symlink()
        and path.name.startswith(CONFIG_NAME + ".")
    )
    if not variants:
        result["status"] = "already-archived" if live.is_file() else "nothing-to-do"
        return result
    if not live.is_file():
        # Promoting a backup would be a guess, and a wrong guess here silently
        # reconfigures the gateway. Make the operator decide, loudly.
        result["status"] = "refused-no-live-config"
        result["reason"] = (
            "%s does not exist, so which of the %d variant(s) was live is not "
            "determinable from the tree; resolve by hand." % (live, len(variants))
        )
        return result

    live_digest = digest(live)
    records = []
    for path in variants:
        records.append(
            {
                "name": path.name,
                "sha256": digest(path),
                "bytes": path.stat().st_size,
                "mtime_utc": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime)
                ),
                "identical_to_live": digest(path) == live_digest,
            }
        )
    result["archived"] = [record["name"] for record in records]

    if not apply:
        result["records"] = records
        return result

    archive.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(archive, 0o700)
    except OSError:
        pass
    for path, record in zip(variants, records):
        target = archive / path.name
        if target.exists():
            record["note"] = "destination existed; left source in place"
            continue
        shutil.copy2(path, target)
        if digest(target) != digest(path):  # pragma: no cover - defensive
            target.unlink(missing_ok=True)
            result["status"] = "verify-failed"
            return result
        path.unlink()
    (archive / "WHICH-WAS-LIVE.md").write_text(
        render_which_was_live(home, live, live_digest, records), encoding="utf-8"
    )
    result["records"] = records
    return result


def render_which_was_live(
    home: Path, live: Path, live_digest: str, records: list
) -> str:
    """The dated note that makes the archive self-explanatory a year from now."""
    lines = [
        "# Gateway config.yaml backup pile — resolved %s (UTC)" % _stamp(),
        "",
        "Archived by `deploy/openclaw/relocate-script-job-home.py` as part of the",
        "Hermes/OpenClaw home untangle. Nothing was deleted: every file below was",
        "moved here from `%s` and can be restored by copying it back." % home,
        "",
        "## Which one was live",
        "",
        "`%s` — the only name the gateway reads — was live and was LEFT IN PLACE." % live,
        "",
        "    sha256  %s" % live_digest,
        "",
        "## Archived variants",
        "",
        "| file | sha256 | bytes | mtime (UTC) | identical to live |",
        "| --- | --- | --- | --- | --- |",
    ]
    for record in records:
        lines.append(
            "| `%s` | `%s` | %d | %s | %s |"
            % (
                record["name"],
                record["sha256"][:16],
                record["bytes"],
                record["mtime_utc"],
                "yes" if record["identical_to_live"] else "no",
            )
        )
    lines += [
        "",
        "A variant marked `identical to live` carried no information the live file",
        "did not; the rest are historical states, kept for forensics only. None of",
        "them is authoritative.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument(
        "operation",
        nargs="?",
        default="scripts",
        choices=("scripts", "config-backups", "all"),
    )
    mode = result.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="perform the moves")
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="report the plan without touching the filesystem (the default)",
    )
    result.add_argument("--source", help="override the legacy scripts directory")
    result.add_argument("--destination", help="override the script-job scripts home")
    result.add_argument("--gateway-home", help="override $HERMES_HOME")
    result.add_argument("--archive-root", help="override $MAC_HOME/backups")
    return result


def main(argv: Optional[list] = None) -> int:
    args = parser().parse_args(argv)
    apply = bool(args.apply)
    results = []
    if args.operation in ("scripts", "all"):
        results.append(
            relocate_scripts(
                Path(args.source).expanduser()
                if args.source
                else legacy_gateway_scripts_dir(),
                Path(args.destination).expanduser()
                if args.destination
                else script_jobs_scripts_dir(),
                apply=apply,
            )
        )
    if args.operation in ("config-backups", "all"):
        results.append(
            archive_config_backups(
                Path(args.gateway_home).expanduser()
                if args.gateway_home
                else gateway_home(),
                Path(args.archive_root).expanduser()
                if args.archive_root
                else backups_dir(),
                apply=apply,
            )
        )
    print(json.dumps(results, indent=2, sort_keys=True))
    # A conflict or a refusal needs an operator, so it must not read as success.
    blocked = [
        item
        for item in results
        if item["status"] in ("conflicts", "verify-failed", "refused-no-live-config")
        or item["status"].startswith("symlink-failed")
    ]
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
