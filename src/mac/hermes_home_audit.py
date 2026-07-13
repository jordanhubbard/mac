"""Point-in-time audit of a Hermes home directory (~/.hermes).

The audit is deliberately read-only.  It inventories:

1. **Scripts** – executables or ``.sh``/``.py`` files found anywhere under
   *home_path* that do not belong to the standard Hermes install tree.
2. **Cron jobs** – structured entries read from ``cron/jobs.json`` (id,
   schedule, command, enabled flag).
3. **Non-standard paths** – top-level entries (and one level deeper for
   well-known container dirs) that are not part of the known Hermes home
   structure.

The result is a plain ``dict`` conforming to schema ``mac.hermes_home_audit.v1``.
The module never imports from ``_hermes`` internal paths and never writes to
disk.

Usage::

    from pathlib import Path
    from mac.hermes_home_audit import audit_hermes_home

    report = audit_hermes_home(Path.home() / ".hermes")
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]

HERMES_HOME_AUDIT_SCHEMA = "mac.hermes_home_audit.v1"

# ---------------------------------------------------------------------------
# Known Hermes home structure
# Top-level names that a standard Hermes install may create or manage.
# A path found in the home that does NOT appear here (or whose first segment
# does not appear here) is flagged as non-standard.
# ---------------------------------------------------------------------------

_KNOWN_TOP_LEVEL: frozenset[str] = frozenset(
    {
        # dotfiles / markers
        ".anthropic_oauth.json",
        ".bin",
        ".container-mode",
        ".env",
        ".install_method",
        ".managed",
        ".skills_prompt_snapshot.json",
        ".update_exit_code",
        ".update_response",
        # config / identity
        "active_profile",
        "auth",
        "auth.json",
        "config.yaml",
        "context_length_cache.yaml",
        "SOUL.md",
        # runtime / gateway
        "gateway.pid",
        "logs",
        "mac-runtime-context.md",
        "processes.json",
        "sandboxes",
        "spawn-trees",
        "state.db",
        "tmp",
        # sessions / chat history
        "channel_directory.json",
        "checkpoints",
        "memories",
        "memory_store.db",
        "response_store.db",
        "sessions",
        # cron
        "cron",
        # skills / plugins / mcp
        "mcp-installs",
        "optional-mcps",
        "optional-skills",
        "plugins",
        "skill-bundles",
        "skills",
        # user scripts
        "bin",
        "hooks",
        "scripts",
        # node / browser toolchain
        "browser_screenshots",
        "byterover",
        "chrome-debug",
        "node",
        "node_modules",
        # media / assets
        "cache",
        "dashboard-themes",
        "disk-cleanup",
        "images",
        "skins",
        "sticker_cache.json",
        # model caches
        "hindsight",
        "honcho.json",
        "mem0.json",
        "modal_snapshots.json",
        "model-providers",
        "models_dev_cache.json",
        "ollama_cloud_models_cache.json",
        "provider_models_cache.json",
        # integrations
        "feishu_comment_pairing.json",
        "feishu_comment_rules.json",
        "feishu_seen_message_ids.json",
        "google_token.json",
        "pastes",
        "slack_accounts.json",
        "slack_channel_teams.json",
        "slack_tokens.json",
        "singularity_snapshots.json",
        "whatsapp",
        # profiles sub-tree
        "profiles",
    }
)

# Directories that are part of the Hermes install itself (skip for script scan)
_HERMES_INSTALL_DIRS: frozenset[str] = frozenset(
    {
        "node_modules",
        "node",
        "mcp-installs",
        "optional-mcps",
        "optional-skills",
        "skill-bundles",
    }
)

# Script file extensions that are interesting
_SCRIPT_EXTENSIONS: frozenset[str] = frozenset({".sh", ".py"})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _is_executable(path: Path) -> bool:
    """Return True if *path* is a file with an executable bit set."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))


def _rel(base: Path, target: Path) -> str:
    try:
        return str(target.relative_to(base))
    except ValueError:
        return str(target)


# ---------------------------------------------------------------------------
# Script inventory
# ---------------------------------------------------------------------------


def _is_script(path: Path) -> bool:
    """Return True when *path* looks like a user/custom script."""
    if not path.is_file():
        return False
    suffix = path.suffix.lower()
    if suffix in _SCRIPT_EXTENSIONS:
        return True
    if _is_executable(path):
        return True
    return False


def _collect_scripts(home: Path) -> list[JsonDict]:
    """Walk *home* and return script entries for non-standard scripts."""
    results: list[JsonDict] = []
    if not home.is_dir():
        return results

    # Directories we descend into for script scanning
    # We skip the Hermes install tree itself (node, mcp-installs, etc.)
    # but DO include user-facing dirs like bin/, hooks/, scripts/, plugins/.
    scan_dirs = [
        home / "bin",
        home / "hooks",
        home / "scripts",
        home / "plugins",
        home / "skills",
    ]
    # Also check top-level files directly (e.g. a stray setup.sh)
    for entry in _safe_iterdir(home):
        if entry.is_file() and _is_script(entry):
            results.append(
                {
                    "path": _rel(home, entry),
                    "executable": _is_executable(entry),
                    "extension": entry.suffix.lower() or None,
                    "source": "home_root",
                }
            )

    seen: set[Path] = set()
    for scan_root in scan_dirs:
        if not scan_root.is_dir():
            continue
        if scan_root in seen:
            continue
        seen.add(scan_root)
        for dirpath, dirnames, filenames in os.walk(scan_root, followlinks=False):
            dp = Path(dirpath)
            # Prune install-managed directories
            dirnames[:] = [
                d for d in dirnames if d not in _HERMES_INSTALL_DIRS and not d.startswith(".")
            ]
            for fname in filenames:
                fpath = dp / fname
                if _is_script(fpath):
                    results.append(
                        {
                            "path": _rel(home, fpath),
                            "executable": _is_executable(fpath),
                            "extension": fpath.suffix.lower() or None,
                            "source": str(scan_root.relative_to(home)),
                        }
                    )

    # Deduplicate by path (top-level file scan and dir scan may overlap for
    # home/scripts/*.py etc.)
    seen_paths: set[str] = set()
    deduped: list[JsonDict] = []
    for item in results:
        p = item["path"]
        if p not in seen_paths:
            seen_paths.add(p)
            deduped.append(item)

    return sorted(deduped, key=lambda e: e["path"])


# ---------------------------------------------------------------------------
# Cron job inventory
# ---------------------------------------------------------------------------


def _load_cron_jobs(home: Path) -> tuple[list[JsonDict], str | None]:
    """Read cron/jobs.json and return (job_list, error_message)."""
    jobs_file = home / "cron" / "jobs.json"
    if not jobs_file.exists():
        return [], None

    try:
        raw = jobs_file.read_text(encoding="utf-8")
    except OSError as exc:
        return [], "read_error: %s" % exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], "json_error: %s" % exc

    if not isinstance(data, list):
        return [], "unexpected_format: top-level value is not a list"

    jobs: list[JsonDict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        schedule_raw = item.get("schedule")
        schedule: str | None
        if isinstance(schedule_raw, dict):
            # Parsed schedule object – normalise to a short string
            stype = str(schedule_raw.get("type") or "").strip()
            expr = str(schedule_raw.get("expression") or schedule_raw.get("cron") or "").strip()
            schedule = ("%s:%s" % (stype, expr)).strip(":") if stype or expr else None
        elif schedule_raw is not None:
            schedule = str(schedule_raw).strip() or None
        else:
            schedule = None

        command_raw = item.get("command") or item.get("prompt") or item.get("task")
        command = str(command_raw).strip() if command_raw is not None else None

        enabled_raw = item.get("enabled")
        if enabled_raw is None:
            enabled = True  # default per Hermes source
        else:
            enabled = bool(enabled_raw)

        jobs.append(
            {
                "id": job_id or None,
                "name": name or None,
                "schedule": schedule,
                "command": command,
                "enabled": enabled,
            }
        )

    return jobs, None


# ---------------------------------------------------------------------------
# Non-standard path inventory
# ---------------------------------------------------------------------------


def _safe_iterdir(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def _collect_nonstandard(home: Path) -> list[JsonDict]:
    """Return entries under *home* that are not part of the known structure."""
    results: list[JsonDict] = []
    if not home.is_dir():
        return results

    for entry in sorted(_safe_iterdir(home)):
        name = entry.name
        if name in _KNOWN_TOP_LEVEL:
            continue

        kind: str
        if entry.is_symlink():
            kind = "symlink"
        elif entry.is_dir():
            kind = "directory"
        elif entry.is_file():
            kind = "file"
        else:
            kind = "other"

        size: int | None = None
        if kind == "file":
            try:
                size = entry.stat().st_size
            except OSError:
                pass

        results.append(
            {
                "path": name,
                "kind": kind,
                "size_bytes": size,
            }
        )

    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def audit_hermes_home(home_path: Path) -> JsonDict:
    """Return a structured audit report for the Hermes home at *home_path*.

    Parameters
    ----------
    home_path:
        Path to the Hermes home directory to audit.  The directory does not
        need to exist; a missing or inaccessible directory is reported in the
        ``status`` field rather than raising.

    Returns
    -------
    dict
        A ``mac.hermes_home_audit.v1`` report dict.  Keys:

        ``schema``
            Always ``"mac.hermes_home_audit.v1"``.
        ``home_path``
            Absolute string path that was audited.
        ``audited_at``
            ISO-8601 UTC timestamp.
        ``home_exists``
            Whether *home_path* is a readable directory.
        ``scripts``
            List of script inventory dicts (path, executable, extension,
            source).
        ``cron_jobs``
            List of cron job summary dicts (id, name, schedule, command,
            enabled).
        ``cron_error``
            ``None`` or an error string if ``cron/jobs.json`` could not be
            read or parsed.
        ``nonstandard_paths``
            List of unexpected top-level entry dicts (path, kind,
            size_bytes).
        ``summary``
            High-level counts: script_count, cron_job_count,
            nonstandard_count, enabled_cron_count.
    """
    home = home_path.resolve() if home_path.is_absolute() else home_path.resolve()
    home_exists = home.is_dir()

    scripts: list[JsonDict] = []
    cron_jobs: list[JsonDict] = []
    cron_error: str | None = None
    nonstandard_paths: list[JsonDict] = []

    if home_exists:
        scripts = _collect_scripts(home)
        cron_jobs, cron_error = _load_cron_jobs(home)
        nonstandard_paths = _collect_nonstandard(home)

    enabled_cron = sum(1 for j in cron_jobs if j.get("enabled"))

    return {
        "schema": HERMES_HOME_AUDIT_SCHEMA,
        "home_path": str(home),
        "audited_at": _utc_now(),
        "home_exists": home_exists,
        "scripts": scripts,
        "cron_jobs": cron_jobs,
        "cron_error": cron_error,
        "nonstandard_paths": nonstandard_paths,
        "summary": {
            "script_count": len(scripts),
            "cron_job_count": len(cron_jobs),
            "enabled_cron_count": enabled_cron,
            "nonstandard_count": len(nonstandard_paths),
        },
    }


__all__ = ["HERMES_HOME_AUDIT_SCHEMA", "audit_hermes_home"]
