"""Phase 1 fleet snapshot — pull / edit / push the editable agent *soul* text.

The fleet's curated identity drifts host-locally with no single source of truth
(e.g. an agent still "knows" a long-dead teammate). Phase 1 captures the
editable TEXT layer — each agent's ``~/.hermes`` soul markdown — into a
git-friendly tree so an operator can edit it and push it back **safely**:

  * pull  -> read SOUL_FILES from every agent into <dir>/agents/<name>/soul/
             plus a manifest.yaml (target, per-file sha256, byte size).
  * edit  -> the operator edits the markdown locally.
  * push  -> diff snapshot vs the agent's CURRENT files, back up the remote
             copy, then write only the files that actually changed. ``dry_run``
             prints the plan and writes nothing.

Out of scope for Phase 1 (referenced as follow-ups): binary memory blobs
(state.db / Qdrant vectors), secrets, and hub-stored persona/mood.

Transport is pluggable. The default :class:`SSHTransport` reaches each agent at
its ``~/.mac/fleets.yaml`` target; tests inject an in-memory fake.
"""

from __future__ import annotations

import hashlib
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

from mac.fleet_ssh import FleetSshSpec, ssh_argv

# The editable text layer. Relative to each agent's Hermes home (~/.hermes).
SOUL_FILES: Tuple[str, ...] = ("SOUL.md", "USER.md", "MEMORY.md")

# Phase 2: the binary memory layer — NOT editable, captured as references
# (agent, date, size, optional sha256) in the manifest, never inlined. Content
# is only transferred on explicit opt-in (these can be gigabytes).
MEMORY_FILES: Tuple[str, ...] = ("state.db", "memory_store.db")

SNAPSHOT_SCHEMA = "mac.soul_snapshot.v1"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport(Protocol):
    """Read/write/backup a file under an agent's Hermes home, keyed by target."""

    def read_text(self, target: str, relpath: str) -> Optional[str]:
        """Return file contents, or None if the file does not exist."""

    def write_text(self, target: str, relpath: str, content: str) -> None:
        """Overwrite the file (caller has already taken a backup)."""

    def backup(self, target: str, relpath: str, *, stamp: str) -> Optional[str]:
        """Copy the current file aside; return the backup path, or None if absent."""

    def stat(self, target: str, relpath: str, *, checksum: bool = False) -> Optional[Dict[str, Any]]:
        """Return {bytes, mtime[, sha256]} for a file WITHOUT transferring it, or
        None if absent. ``checksum`` adds sha256 (reads the file; can be slow on
        large blobs)."""


class SSHTransport:
    """Transport over ssh, relative to ``$HOME/.hermes`` on each target host.

    ``stamp`` is supplied by the caller (not generated here) so a snapshot's
    backups share one timestamp and the module stays deterministic/testable.
    """

    def __init__(
        self,
        *,
        hermes_subdir: str = ".hermes",
        connect_timeout: int = 10,
        ssh_extra: Optional[Sequence[str]] = None,
        routes: Optional[Mapping[str, FleetSshSpec]] = None,
    ) -> None:
        self._sub = hermes_subdir
        self._connect_timeout = connect_timeout
        self._routes = dict(routes or {})
        self._ssh = [
            "ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}",
            *(ssh_extra or []),
        ]

    def _argv(self, target: str, command: str) -> List[str]:
        route = self._routes.get(target)
        if route is not None:
            return ssh_argv(route, command, connect_timeout=self._connect_timeout)
        # Library compatibility for callers that construct a transport
        # directly. Production fleet CLI paths always provide canonical routes.
        return [*self._ssh, target, command]

    def _remote(self, relpath: str) -> str:
        # Single-quoted for the remote shell; relpath is a fixed allowlisted name.
        return '"$HOME/%s/%s"' % (self._sub, relpath)

    def read_text(self, target: str, relpath: str) -> Optional[str]:
        # The remote command is passed as ONE argument; ssh hands it to the
        # remote login shell verbatim. (Passing "sh","-c",script as separate
        # argv items makes ssh flatten them and lose the quoting.)
        path = self._remote(relpath)
        cmd = "if [ -f %s ]; then cat %s; else exit 7; fi" % (path, path)
        proc = subprocess.run(self._argv(target, cmd), capture_output=True, text=True)
        if proc.returncode == 7:
            return None
        if proc.returncode != 0:
            raise RuntimeError("read %s on %s failed: %s" % (relpath, target, proc.stderr.strip()))
        return proc.stdout

    def backup(self, target: str, relpath: str, *, stamp: str) -> Optional[str]:
        path = self._remote(relpath)
        bak = '"$HOME/%s/%s.bak.%s"' % (self._sub, relpath, stamp)
        cmd = "if [ -f %s ]; then cp -f %s %s && echo COPIED; fi" % (path, path, bak)
        proc = subprocess.run(self._argv(target, cmd), capture_output=True, text=True)
        if proc.returncode != 0:
            raise RuntimeError("backup %s on %s failed: %s" % (relpath, target, proc.stderr.strip()))
        return ("%s.bak.%s" % (relpath, stamp)) if "COPIED" in proc.stdout else None

    def write_text(self, target: str, relpath: str, content: str) -> None:
        path = self._remote(relpath)
        cmd = 'mkdir -p "$(dirname %s)" && cat > %s' % (path, path)
        proc = subprocess.run(
            self._argv(target, cmd), input=content, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError("write %s on %s failed: %s" % (relpath, target, proc.stderr.strip()))

    def stat(self, target: str, relpath: str, *, checksum: bool = False) -> Optional[Dict[str, Any]]:
        path = self._remote(relpath)
        # Emit size+mtime on one line (instant), sha256 on the next (opt-in; it
        # reads the whole file). Direct commands — no nested echo "$(...)" whose
        # quoting gets mangled over ssh.
        sha = (" sha256sum %s | cut -d' ' -f1;" % path) if checksum else ""
        cmd = 'if [ -f %s ]; then stat -c "%%s %%Y" %s;%s else exit 7; fi' % (path, path, sha)
        proc = subprocess.run(self._argv(target, cmd), capture_output=True, text=True)
        if proc.returncode == 7:
            return None
        if proc.returncode != 0:
            raise RuntimeError("stat %s on %s failed: %s" % (relpath, target, proc.stderr.strip()))
        parts = proc.stdout.split()  # "<size> <mtime>[ <sha>]" across 1-2 lines
        meta: Dict[str, Any] = {"present": True}
        if len(parts) >= 2 and parts[0].isdigit():
            meta["bytes"] = int(parts[0])
            meta["mtime"] = int(parts[1])
        if checksum and len(parts) >= 3:
            meta["sha256"] = parts[2]
        return meta


# ---------------------------------------------------------------------------
# Fleet roster
# ---------------------------------------------------------------------------


def load_fleet_agents(fleets_config: dict, fleet_name: str) -> List[Tuple[str, str]]:
    """Return [(agent_name, ssh_target), ...] for *fleet_name* from a fleets.yaml dict."""
    fleets = (fleets_config or {}).get("fleets") or {}
    fleet = fleets.get(fleet_name)
    if not fleet:
        raise KeyError("fleet %r not found (have: %s)" % (fleet_name, sorted(fleets)))
    out: List[Tuple[str, str]] = []
    for agent in fleet.get("agents") or []:
        name = str(agent.get("name") or "").strip()
        target = str(agent.get("target") or "").strip()
        if name and target:
            out.append((name, target))
    return out


# ---------------------------------------------------------------------------
# Pull
# ---------------------------------------------------------------------------


def pull_snapshot(
    agents: Sequence[Tuple[str, str]],
    dest_dir: Path,
    transport: Transport,
    *,
    fleet: str,
    pulled_at: str,
    soul_files: Sequence[str] = SOUL_FILES,
    memory_files: Sequence[str] = MEMORY_FILES,
    memory_checksum: bool = False,
) -> dict:
    """Pull SOUL_FILES from each agent into ``dest_dir`` and return the manifest.

    Writes ``<dest>/agents/<name>/soul/<file>`` for present files and a
    ``<dest>/manifest.yaml``. ``pulled_at`` is supplied by the caller.

    Also records the binary memory layer (MEMORY_FILES) as **references** under
    each agent's ``memory`` key — {present, bytes, mtime[, sha256]} — WITHOUT
    transferring the (potentially gigabyte) content. ``memory_checksum`` adds a
    sha256 (reads the remote blob; slower).
    """
    dest_dir = Path(dest_dir)
    manifest: dict = {
        "schema": SNAPSHOT_SCHEMA,
        "layer": "soul+memory-refs",
        "fleet": fleet,
        "pulled_at": pulled_at,
        "soul_files": list(soul_files),
        "memory_files": list(memory_files),
        "agents": {},
    }
    for name, target in agents:
        soul_dir = dest_dir / "agents" / name / "soul"
        soul_dir.mkdir(parents=True, exist_ok=True)
        files_meta: Dict[str, dict] = {}
        for rel in soul_files:
            content = transport.read_text(target, rel)
            if content is None:
                files_meta[rel] = {"present": False}
                continue
            (soul_dir / rel).write_text(content, encoding="utf-8")
            files_meta[rel] = {
                "present": True,
                "sha256": _sha256(content),
                "bytes": len(content.encode("utf-8")),
            }
        # Binary memory: reference-only (metadata), never inlined.
        memory_meta: Dict[str, dict] = {}
        for rel in memory_files:
            meta = transport.stat(target, rel, checksum=memory_checksum)
            memory_meta[rel] = meta if meta is not None else {"present": False}
        manifest["agents"][name] = {
            "target": target, "files": files_meta, "memory": memory_meta,
        }
    return manifest


# ---------------------------------------------------------------------------
# Phase 3 — hub-stored persona + mood
# ---------------------------------------------------------------------------


def _persona_for(personas: Sequence[Dict[str, Any]], name: str) -> Optional[Dict[str, Any]]:
    """Match the agent's persona by name (exact, or the ``persona_<name>``/
    ``<name>`` convention) from a list_personas() result."""
    want = {name.lower(), ("persona_%s" % name).lower(), name.lower().replace("persona_", "")}
    for p in personas:
        pn = str((p or {}).get("name") or "").lower()
        if pn in want or pn.replace("persona_", "") == name.lower():
            return p
    return None


def capture_hub_state(
    hub: Any,
    agents: Sequence[Tuple[str, str]],
    dest_dir: Path,
    *,
    pulled_at: str,
) -> dict:
    """Capture each agent's HUB-stored persona + current mood into the snapshot.

    ``hub`` exposes ``list_personas()`` and ``get_current_mood(agent_id)`` (the
    RemoteDispatch surface; tests inject a fake). ``agents`` is [(name,
    agent_id)]. Writes ``agents/<name>/persona.yaml`` and ``mood.yaml`` (text,
    editable) and returns the manifest section. Best-effort per field — a hub
    error for one agent doesn't abort the others.
    """
    dest_dir = Path(dest_dir)
    try:
        personas = [_as_plain(p) for p in (hub.list_personas() or [])]
    except Exception:  # noqa: BLE001 — persona list is best-effort
        personas = []
    out: Dict[str, dict] = {"pulled_at": pulled_at, "agents": {}}
    for name, agent_id in agents:
        agent_dir = dest_dir / "agents" / name
        agent_dir.mkdir(parents=True, exist_ok=True)
        section: Dict[str, Any] = {"agent_id": agent_id}
        persona = _persona_for(personas, name)
        if persona is not None:
            (agent_dir / "persona.yaml").write_text(_yaml_dump(persona), encoding="utf-8")
            section["persona"] = {"present": True, "name": persona.get("name")}
        else:
            section["persona"] = {"present": False}
        try:
            mood = _as_plain(hub.get_current_mood(agent_id)) or None
        except Exception:  # noqa: BLE001 — mood is best-effort
            mood = None
        if mood:
            (agent_dir / "mood.yaml").write_text(_yaml_dump(mood), encoding="utf-8")
            section["mood"] = {"present": True}
        else:
            section["mood"] = {"present": False}
        out["agents"][name] = section
    return out


def _as_plain(obj: Any) -> Any:
    """Normalize a hub client result to a plain dict. RemoteDispatch returns
    _Dictish wrappers (have ``to_dict``); a fake/LocalDispatch returns dicts.
    ``dict(_Dictish)`` does NOT unwrap, so match on ``to_dict`` first."""
    if obj is None:
        return None
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return obj
    try:
        return dict(obj)
    except Exception:  # noqa: BLE001
        return obj


def _yaml_dump(obj: Any) -> str:
    import yaml  # local import keeps the module import light for the SSH path
    return yaml.safe_dump(obj, sort_keys=False, default_flow_style=False)


# ---------------------------------------------------------------------------
# Push (diff -> backup -> write)
# ---------------------------------------------------------------------------


@dataclass
class FileChange:
    agent: str
    target: str
    relpath: str
    status: str  # "changed" | "new" | "unchanged" | "missing_in_snapshot"
    local_sha: Optional[str] = None
    remote_sha: Optional[str] = None
    backup_path: Optional[str] = None
    applied: bool = False


@dataclass
class PushResult:
    changes: List[FileChange] = field(default_factory=list)
    dry_run: bool = True

    @property
    def to_apply(self) -> List[FileChange]:
        return [c for c in self.changes if c.status in ("changed", "new")]


def plan_and_push(
    snapshot_dir: Path,
    manifest: dict,
    transport: Transport,
    *,
    stamp: str,
    dry_run: bool = True,
    only_agents: Optional[Sequence[str]] = None,
) -> PushResult:
    """Diff each snapshot soul file against the agent's CURRENT file and, unless
    ``dry_run``, back up + write the ones that changed.

    Safety: never deletes; only writes files present in the snapshot whose
    content differs from the live file. A file present live but absent from the
    snapshot is reported (``missing_in_snapshot``) and left untouched.
    """
    snapshot_dir = Path(snapshot_dir)
    result = PushResult(dry_run=dry_run)
    agents_meta = (manifest or {}).get("agents") or {}
    want = set(only_agents) if only_agents else None
    for name, meta in agents_meta.items():
        if want is not None and name not in want:
            continue
        target = str(meta.get("target") or "")
        for rel, fmeta in (meta.get("files") or {}).items():
            if not fmeta.get("present"):
                continue  # not captured at pull time -> nothing to push
            local_path = snapshot_dir / "agents" / name / "soul" / rel
            if not local_path.exists():
                continue
            local = local_path.read_text(encoding="utf-8")
            local_sha = _sha256(local)
            remote = transport.read_text(target, rel)
            remote_sha = _sha256(remote) if remote is not None else None
            if remote is None:
                status = "new"
            elif remote_sha == local_sha:
                status = "unchanged"
            else:
                status = "changed"
            change = FileChange(
                agent=name, target=target, relpath=rel, status=status,
                local_sha=local_sha, remote_sha=remote_sha,
            )
            if status in ("changed", "new") and not dry_run:
                change.backup_path = transport.backup(target, rel, stamp=stamp)
                transport.write_text(target, rel, local)
                change.applied = True
            result.changes.append(change)
    return result
