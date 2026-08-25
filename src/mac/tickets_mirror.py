"""Optional local ``.tickets/<id>.md`` mirror emission.

parity-tickets-autoemit-01: `bd` kept its git-distributed mirror in sync with
every change; `mac task create/close` historically only wrote `.tickets/` during
`migrate-beads`, so optional local mirrors drifted from the ledger. This module
renders a wedow-compatible ticket from a mac task dict — reusing the migrator's
renderer so the format never drifts — and writes it into the repo's existing
`.tickets/` directory.

Emits only when a `.tickets/` dir already exists (git repo root, else cwd); it
never creates one. Opt out per-call (`--no-ticket`) or globally via
`MAC_NO_TICKET_MIRROR`. The MAC task ledger remains canonical; `.tickets/` is
ignored local operational state in this repo.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from mac.beads_migrator import _render_ticket


def render_ticket(task: Dict[str, Any], *, close_reason: Optional[str] = None) -> str:
    """Render the `.tickets/<id>.md` content for a mac task dict."""
    metadata = task.get("metadata") or {}
    issue: Dict[str, Any] = {
        "id": task.get("id"),
        "status": task.get("state") or "open",
        # mac task deps are a flat id list; the renderer wants typed dep dicts.
        "dependencies": [
            {"type": "blocks", "depends_on_id": dep} for dep in (task.get("dependencies") or [])
        ],
        "created_at": task.get("created_at") or "",
        "issue_type": metadata.get("type") or "task",
        "priority": task.get("priority"),
        "title": task.get("title") or task.get("id"),
        "description": task.get("description") or "",
    }
    if close_reason:
        issue["close_reason"] = close_reason
    return _render_ticket(issue, str(task.get("id")))


def tickets_dir() -> Optional[Path]:
    """The `.tickets/` directory to mirror into — the git repo root's, else the
    cwd's. Returns None when no `.tickets/` exists (we never create one)."""
    candidates = []
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if top.returncode == 0 and top.stdout.strip():
            candidates.append(Path(top.stdout.strip()) / ".tickets")
    except Exception:
        pass
    try:
        candidates.append(Path(os.getcwd()) / ".tickets")
    except OSError:
        pass
    for directory in candidates:
        if directory.is_dir():
            return directory
    return None


def emit(task: Dict[str, Any], *, close_reason: Optional[str] = None) -> Optional[Path]:
    """Write/update `.tickets/<id>.md` for a mac task.

    No-op (returns None) when disabled via ``MAC_NO_TICKET_MIRROR``, when there
    is no `.tickets/` directory, or when the file is already up to date.
    """
    if os.environ.get("MAC_NO_TICKET_MIRROR"):
        return None
    task_id = task.get("id")
    if not task_id:
        return None
    directory = tickets_dir()
    if directory is None:
        return None
    path = directory / ("%s.md" % task_id)
    content = render_ticket(task, close_reason=close_reason)
    if path.exists() and path.read_text(encoding="utf-8") == content:
        return None
    path.write_text(content, encoding="utf-8")
    return path
