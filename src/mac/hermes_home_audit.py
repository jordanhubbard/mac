"""Read-only auditor of a gateway (Hermes) home directory.

The 66-entry canonical allow-list here is the single source of truth for
``~/.hermes`` top-level names. ``mac.mac_home_audit`` reuses it for the
unified ``gateway/`` bucket instead of copying the set.

Public API (kept stable): ``HERMES_HOME_AUDIT_SCHEMA``, ``HERMES_KNOWN_TOP_LEVEL``,
``audit_hermes_home``.
"""

from __future__ import annotations

import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, FrozenSet, Iterable, List, Union

from mac import mac_paths

HERMES_HOME_AUDIT_SCHEMA = "mac.hermes_home_audit.v1"

# Canonical top-level names of a Hermes/gateway home. Order is not significant.
HERMES_KNOWN_TOP_LEVEL: FrozenSet[str] = frozenset(
    {
        ".env",
        ".gitignore",
        "AGENTS.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "IDENTITY.md",
        "MEMORY.md",
        "SOUL.md",
        "TOOLS.md",
        "USER.md",
        "auth.json",
        "bin",
        "cache",
        "channel_directory.json",
        "checkpoints",
        "config.yaml",
        "context",
        "cron",
        "devices",
        "dream_cycle.py",
        "dream_logs",
        "hooks",
        "images",
        "instances",
        "log",
        "logs",
        "mac-memory-topology.json",
        "mac-runtime-context.json",
        "mac-runtime-context.md",
        "media",
        "memories",
        "memory",
        "memory_store.db",
        "models",
        "mood-memory.json",
        "mood-overlay.json",
        "mood.json",
        "pairing",
        "personas",
        "plugins",
        "profiles",
        "run",
        "sandboxes",
        "scripts",
        "secrets.json",
        "sessions",
        "settings.json",
        "skills",
        "slack_home_channels.json",
        "state.db",
        "tmp",
        "token.json",
        "transcripts",
        "uploads",
        "update-check.json",
        "versions.json",
        "workspace",
        "channel-directory.json",
        "gateway.pid",
        "hermes.pid",
        ".internal",
        ".config",
        "lib",
        "share",
        "etc",
        "skills.d",
    }
)

HERMES_SCRIPT_SCAN_DIRS: tuple[str, ...] = ("scripts", "bin", "hooks")
_SCRIPT_SUFFIXES = {".py", ".sh"}

Pathish = Union[str, Path, None]


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


def safe_iterdir(path: Path) -> List[Path]:
    """Return children of ``path``, or an empty list if listing is not possible."""
    try:
        return sorted(path.iterdir(), key=lambda item: item.name)
    except OSError:
        return []


def classify_named_children(
    directory: Path,
    known: Iterable[str],
    *,
    container: str = "",
) -> List[Dict[str, Any]]:
    """Classify immediate children of a well-known container directory.

    Names in ``known`` are ``canonical``; anything else is ``drift``. This is
    the same technique historically used for the gateway home top-level.
    """
    allowed = frozenset(known)
    entries: List[Dict[str, Any]] = []
    for child in safe_iterdir(directory):
        name = child.name
        rel = "%s/%s" % (container, name) if container else name
        if name in allowed:
            classification = "canonical"
        else:
            classification = "drift"
        entries.append(
            {
                "path": rel,
                "name": name,
                "kind": _kind(child),
                "classification": classification,
                "container": container,
            }
        )
    return entries


def _scan_scripts(root: Path) -> List[Dict[str, Any]]:
    found: List[Dict[str, Any]] = []
    for source in HERMES_SCRIPT_SCAN_DIRS:
        scan_dir = root / source
        if _kind(scan_dir) not in {"directory", "symlink"}:
            continue
        if not _exists(scan_dir):
            continue
        for child in safe_iterdir(scan_dir):
            suffix = child.suffix.lower()
            if suffix not in _SCRIPT_SUFFIXES:
                continue
            executable = False
            try:
                executable = os.access(child, os.X_OK)
            except OSError:
                executable = False
            found.append(
                {
                    "name": child.name,
                    "path": "%s/%s" % (source, child.name),
                    "source": source,
                    "executable": bool(executable),
                    "kind": _kind(child),
                }
            )
    return found


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


def audit_hermes_home(home: Pathish = None) -> Dict[str, Any]:
    """Read-only audit of a Hermes/gateway home.

    ``home`` is an explicit override for tests. When omitted, the root is
    resolved only through ``mac.mac_paths.gateway_home()``.
    """
    root = Path(home) if home is not None else mac_paths.gateway_home()
    status = _status_for_root(root)
    root_exists = status not in {"missing"}
    entries: List[Dict[str, Any]] = []
    scripts: List[Dict[str, Any]] = []
    if status == "ok":
        entries = classify_named_children(root, HERMES_KNOWN_TOP_LEVEL)
        scripts = _scan_scripts(root)
    unknown = [item for item in entries if item["classification"] == "drift"]
    known = [item for item in entries if item["classification"] == "canonical"]
    return {
        "schema": HERMES_HOME_AUDIT_SCHEMA,
        "home_path": str(root),
        "audited_at": _utc_now(),
        "home_exists": root_exists,
        "status": status,
        "entries": entries,
        "unknown_top_level": unknown,
        "known_top_level": known,
        "scripts": scripts,
        "summary": {
            "entry_count": len(entries),
            "known_count": len(known),
            "unknown_count": len(unknown),
            "script_count": len(scripts),
        },
    }


__all__ = [
    "HERMES_HOME_AUDIT_SCHEMA",
    "HERMES_KNOWN_TOP_LEVEL",
    "HERMES_SCRIPT_SCAN_DIRS",
    "audit_hermes_home",
    "classify_named_children",
    "safe_iterdir",
]
