"""Agent memory journaling (journal-01).

Daily snapshots of an agent's *emergent* state — SOUL.md, USER.md, MEMORY.md,
the memories/ directory, mood state, and the persona config — into
``$HOME/.mac/journal/<date>/``, each with a manifest, plus an optional backup
hook so the snapshot can be archived off-host (e.g. to a cloud blob store).

This exists because an agent's evolved personality is irreplaceable. If those
files are lost (a bad redeploy, a wiped host, an over-eager seed of the generic
default soul), there is no way to regenerate the accumulated state — and the
people who work with that agent genuinely feel the loss. Journaling makes the
state restorable, and the hook lets the hub ship copies to durable storage.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

# The files/dirs that together make up an agent's "soul + memory". Missing ones
# are skipped — not every agent (or layout) has every file.
STATE_ENTRIES: List[str] = [
    "SOUL.md",            # identity / voice
    "USER.md",            # what it knows about its human
    "MEMORY.md",          # persistent notes
    "memories",           # dir: daily memories (+ MEMORY.md/USER.md in newer layout)
    "mood-overlay.json",  # active mood overlay (mood engine)
    "mood-memory.json",   # per-actor warmth/anger memory (mood engine)
    "config.yaml",        # persona / model config
]


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def journal_root() -> Path:
    return Path(os.environ.get("MAC_JOURNAL_DIR") or (Path.home() / ".mac" / "journal"))


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _agent_id() -> str:
    return (
        os.environ.get("MAC_AGENT_ID")
        or os.environ.get("MAC_AGENT_NAME")
        or os.environ.get("HERMES_INSTANCE_ID")
        or "agent"
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot(
    *,
    home: Optional[Path] = None,
    root: Optional[Path] = None,
    date: Optional[str] = None,
    agent_id: Optional[str] = None,
    run_hook: bool = True,
) -> Dict[str, Any]:
    """Copy the agent's soul+memory state into ``<root>/<date>/`` and write a
    manifest (with per-file sha256). Runs the backup hook unless run_hook=False.
    Idempotent for a given date (re-running refreshes that day's snapshot)."""
    home = Path(home) if home else hermes_home()
    root = Path(root) if root else journal_root()
    date = date or _today()
    agent_id = agent_id or _agent_id()
    dest = root / date
    dest.mkdir(parents=True, exist_ok=True)

    captured: List[str] = []
    for name in STATE_ENTRIES:
        src = home / name
        if not src.exists():
            continue
        target = dest / name
        if src.is_dir():
            shutil.copytree(src, target, dirs_exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        captured.append(name)

    files: Dict[str, str] = {}
    for p in sorted(dest.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            files[str(p.relative_to(dest))] = _sha256(p)

    manifest = {
        "version": "mac.agent_journal.v1",
        "date": date,
        "agent_id": agent_id,
        "hermes_home": str(home),
        "captured": captured,
        "files": files,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    manifest["hook"] = _run_backup_hook(dest, manifest) if run_hook else None
    return manifest


def _run_backup_hook(dest: Path, manifest: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Invoke ``MAC_JOURNAL_BACKUP_HOOK`` (a shell command) so an operator can
    archive the snapshot off-host — e.g. ``aws s3 cp``, ``gsutil``, ``rclone``,
    or a hub aggregator. The hook gets the snapshot location via env:
    MAC_JOURNAL_PATH / _DATE / _AGENT / _MANIFEST. Best-effort: a hook failure is
    recorded in the manifest but does NOT fail the snapshot (the local journal
    is the source of truth; the hook is an extra copy)."""
    hook = (os.environ.get("MAC_JOURNAL_BACKUP_HOOK") or "").strip()
    if not hook:
        return None
    env = dict(os.environ)
    env.update(
        {
            "MAC_JOURNAL_PATH": str(dest),
            "MAC_JOURNAL_DATE": str(manifest.get("date")),
            "MAC_JOURNAL_AGENT": str(manifest.get("agent_id")),
            "MAC_JOURNAL_MANIFEST": str(dest / "manifest.json"),
        }
    )
    try:
        proc = subprocess.run(
            ["/bin/sh", "-c", hook],
            env=env,
            capture_output=True,
            text=True,
            timeout=600,
        )
        result: Dict[str, Any] = {"ran": True, "exit_code": proc.returncode}
        if proc.returncode != 0:
            result["stderr"] = (proc.stderr or "")[-500:]
        return result
    except Exception as exc:  # noqa: BLE001 - hook is best-effort
        return {"ran": True, "error": str(exc)[:200]}


def list_journals(root: Optional[Path] = None) -> List[Dict[str, Any]]:
    root = Path(root) if root else journal_root()
    if not root.exists():
        return []
    out: List[Dict[str, Any]] = []
    for d in sorted(p for p in root.iterdir() if p.is_dir()):
        mf = d / "manifest.json"
        if not mf.exists():
            continue
        try:
            m = json.loads(mf.read_text())
            out.append(
                {
                    "date": m.get("date") or d.name,
                    "agent_id": m.get("agent_id"),
                    "files": len(m.get("files") or {}),
                    "path": str(d),
                }
            )
        except Exception:  # noqa: BLE001
            out.append({"date": d.name, "agent_id": None, "files": 0, "path": str(d)})
    return out


def restore(
    date: str,
    *,
    home: Optional[Path] = None,
    root: Optional[Path] = None,
    dry_run: bool = False,
) -> Dict[str, Any]:
    """Restore an agent's state from a journal date back into HERMES_HOME. Always
    snapshots the *current* state first (a ``pre-restore-*`` journal) so a
    restore is itself reversible — restoring a soul should never destroy one."""
    home = Path(home) if home else hermes_home()
    root = Path(root) if root else journal_root()
    src = root / date
    mf = src / "manifest.json"
    if not mf.exists():
        raise FileNotFoundError("no journal for %s under %s" % (date, root))
    manifest = json.loads(mf.read_text())
    plan = sorted((manifest.get("files") or {}).keys())
    if dry_run:
        return {"dry_run": True, "date": date, "would_restore": plan, "into": str(home)}
    safety = snapshot(home=home, root=root, date="pre-restore-%s" % _today(), run_hook=False)
    for rel in plan:
        sp = src / rel
        dp = home / rel
        dp.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sp, dp)
    return {"restored": plan, "from": date, "into": str(home), "safety_backup": safety.get("date")}
