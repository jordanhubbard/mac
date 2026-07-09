#!/usr/bin/env python3
"""Reversibly import Hermes identity and memory into an OpenClaw workspace.

The original ``~/.hermes`` tree is never modified.  Imports are idempotent and
conflict-aware: after the first import, a destination file is updated only when
it still matches the hash written by the previous import.  Locally edited
OpenClaw files are preserved and the new candidate is written below the private
migration directory for an operator to reconcile.

This utility intentionally uses only the Python standard library so it can run
on every fleet node before the OpenShell image starts.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
from typing import Any, Iterable, Iterator


SCHEMA = "mac.openclaw_continuity_migration.v1"
MANIFEST_NAME = "manifest.json"
HIGH_CONFIDENCE_SECRETS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{16,}\b"),
    re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\b[0-9]{6,12}:[A-Za-z0-9_-]{25,}\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{20,}"),
)
ASSIGNMENT_SECRET = re.compile(
    r"(?im)^(?P<prefix>\s*(?:export\s+)?"
    r"[A-Za-z0-9_.-]*(?:token|secret|password|api[_-]?key)"
    r"[A-Za-z0-9_.-]*\s*[:=]\s*)(?P<value>[^\s#][^\r\n]*)$"
)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def digest_tree(path: Path) -> str:
    hasher = hashlib.sha256()
    if not path.exists():
        return digest_bytes(b"")
    for child in sorted(item for item in path.rglob("*") if item.is_file()):
        rel = child.relative_to(path).as_posix().encode("utf-8")
        hasher.update(len(rel).to_bytes(8, "big"))
        hasher.update(rel)
        hasher.update(bytes.fromhex(digest_file(child)))
    return hasher.hexdigest()


def redact(text: str) -> tuple[str, int]:
    count = 0
    for pattern in HIGH_CONFIDENCE_SECRETS:
        text, replaced = pattern.subn("[REDACTED_SECRET]", text)
        count += replaced

    def replace_assignment(match: re.Match[str]) -> str:
        nonlocal count
        value = match.group("value").strip()
        if value.startswith(("${", "$", "<", "[REDACTED_")):
            return match.group(0)
        if value.lower() in {"none", "null", "false", "true", "changeme", "example"}:
            return match.group(0)
        count += 1
        return match.group("prefix") + "[REDACTED_SECRET]"

    return ASSIGNMENT_SECRET.sub(replace_assignment, text), count


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (FileNotFoundError, IsADirectoryError, UnicodeDecodeError, OSError):
        return None


def atomic_write(path: Path, data: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, mode)
        os.replace(name, path)
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def load_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


class Importer:
    def __init__(
        self,
        *,
        hermes_home: Path,
        workspace: Path,
        state_dir: Path,
        migration_dir: Path,
        agent_id: str,
        public_identity: str,
        proposal: Path | None,
    ) -> None:
        self.hermes_home = hermes_home
        self.workspace = workspace
        self.state_dir = state_dir
        self.migration_dir = migration_dir
        self.agent_id = agent_id
        self.public_identity = public_identity
        self.proposal = proposal
        self.manifest_path = migration_dir / MANIFEST_NAME
        self.previous = load_json(self.manifest_path, {})
        self.previous_files: dict[str, str] = dict(self.previous.get("managed_files") or {})
        self.managed_files: dict[str, str] = {}
        self.conflicts: list[dict[str, str]] = []
        self.redaction_count = 0
        self.source_hashes: dict[str, str] = {}
        self.counts: dict[str, int] = defaultdict(int)

    def _candidate_path(self, relative: str) -> Path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return self.migration_dir / "conflicts" / stamp / relative

    def install_text(self, relative: str, content: str, *, source: str) -> None:
        content, redactions = redact(content)
        self.redaction_count += redactions
        if not content.endswith("\n"):
            content += "\n"
        encoded = content.encode("utf-8")
        proposed_hash = digest_bytes(encoded)
        destination = self.workspace / relative
        current_hash = digest_file(destination) if destination.is_file() else None
        previous_hash = self.previous_files.get(relative)
        if current_hash is not None and current_hash != proposed_hash:
            if previous_hash is None or current_hash != previous_hash:
                candidate = self._candidate_path(relative)
                atomic_write(candidate, content)
                self.conflicts.append(
                    {
                        "path": relative,
                        "source": source,
                        "preserved_hash": current_hash,
                        "candidate_hash": proposed_hash,
                        "candidate": str(candidate),
                    }
                )
                self.managed_files[relative] = current_hash
                return
        atomic_write(destination, content)
        self.managed_files[relative] = proposed_hash
        source_path = Path(source)
        self.source_hashes[source] = (
            digest_file(source_path) if source_path.is_file() else digest_bytes(encoded)
        )

    def _authoritative_hermes_file(self, name: str) -> tuple[Path | None, list[Path]]:
        candidates = [self.hermes_home / "memories" / name, self.hermes_home / name]
        existing = [path for path in candidates if path.is_file()]
        return (existing[0] if existing else None), existing[1:]

    def import_identity(self) -> None:
        soul, legacy_souls = self._authoritative_hermes_file("SOUL.md")
        user, legacy_users = self._authoritative_hermes_file("USER.md")
        memory, legacy_memories = self._authoritative_hermes_file("MEMORY.md")
        if soul:
            self.install_text("SOUL.md", soul.read_text(encoding="utf-8", errors="replace"), source=str(soul))
        if user:
            self.install_text("USER.md", user.read_text(encoding="utf-8", errors="replace"), source=str(user))
        if memory:
            self.install_text("MEMORY.md", memory.read_text(encoding="utf-8", errors="replace"), source=str(memory))

        name = self.public_identity or self.agent_id.removeprefix("agent_")
        identity = (
            "# IDENTITY.md\n\n"
            f"- **Name:** {name}\n"
            f"- **Fleet agent ID:** {self.agent_id}\n"
            "- **Continuity:** Migrated from the preserved Hermes identity; "
            "SOUL.md remains the authoritative personality.\n"
        )
        self.install_text("IDENTITY.md", identity, source="generated:hermes-identity")
        marker = "MAC_CONTINUITY_%s" % hashlib.sha256(self.agent_id.encode("utf-8")).hexdigest()[:16]
        self.install_text(
            "memory/continuity-acceptance.md",
            "# MAC continuity acceptance marker\n\n"
            f"This private marker proves that OpenClaw indexed the durable workspace: {marker}\n",
            source="generated:continuity-acceptance",
        )

        for path in [*legacy_souls, *legacy_users, *legacy_memories]:
            authoritative = {"SOUL.md": soul, "USER.md": user, "MEMORY.md": memory}.get(path.name)
            if authoritative and digest_file(path) == digest_file(authoritative):
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            self.install_text(
                f"memory/hermes-legacy/{path.parent.name}-{path.name}",
                "# Preserved non-authoritative Hermes variant\n\n" + content,
                source=str(path),
            )

        legacy_agents = self.hermes_home / "AGENTS.md"
        if legacy_agents.is_file():
            self.install_text(
                "memory/hermes-legacy/AGENTS.md",
                "# Preserved Hermes AGENTS.md\n\n"
                + legacy_agents.read_text(encoding="utf-8", errors="replace"),
                source=str(legacy_agents),
            )

        bootstrap = self.workspace / "BOOTSTRAP.md"
        if bootstrap.exists():
            bootstrap.unlink()

    def import_proposal(self) -> None:
        proposal = load_json(self.proposal or Path("/nonexistent"), None)
        if not isinstance(proposal, dict):
            raise ValueError("new agent has no valid mentor personality proposal")
        required = ("name", "role", "vibe", "emoji", "soul", "user", "memory")
        missing = [key for key in required if not str(proposal.get(key) or "").strip()]
        if missing:
            raise ValueError("mentor personality proposal is missing: %s" % ", ".join(missing))
        name = str(proposal["name"]).strip()
        identity = (
            "# IDENTITY.md\n\n"
            f"- **Name:** {name}\n"
            f"- **Role:** {str(proposal['role']).strip()}\n"
            f"- **Vibe:** {str(proposal['vibe']).strip()}\n"
            f"- **Emoji:** {str(proposal['emoji']).strip()}\n"
            f"- **Fleet agent ID:** {self.agent_id}\n"
            f"- **Mentor:** {str(proposal.get('mentor_agent_id') or 'fleet mentor').strip()}\n"
        )
        self.install_text("IDENTITY.md", identity, source=str(self.proposal))
        self.install_text("SOUL.md", str(proposal["soul"]), source=str(self.proposal))
        self.install_text("USER.md", str(proposal["user"]), source=str(self.proposal))
        self.install_text("MEMORY.md", str(proposal["memory"]), source=str(self.proposal))
        marker = "MAC_CONTINUITY_%s" % hashlib.sha256(self.agent_id.encode("utf-8")).hexdigest()[:16]
        self.install_text(
            "memory/continuity-acceptance.md",
            "# MAC continuity acceptance marker\n\n"
            f"This private marker proves that OpenClaw indexed the durable workspace: {marker}\n",
            source="generated:continuity-acceptance",
        )
        atomic_write(
            self.migration_dir / "personality-provenance.json",
            json.dumps(
                {
                    "schema": "mac.openclaw_personality_provenance.v1",
                    "agent_id": self.agent_id,
                    "name": name,
                    "mentor_agent_id": proposal.get("mentor_agent_id"),
                    "created_at": proposal.get("created_at") or utcnow(),
                    "proposal_sha256": digest_file(self.proposal),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )

    def import_skills(self) -> None:
        source_root = self.hermes_home / "skills"
        if not source_root.is_dir():
            return
        for source in sorted(source_root.rglob("*")):
            if not source.is_file() or source.is_symlink():
                continue
            relative = source.relative_to(source_root).as_posix()
            try:
                content = source.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                # Preserve non-text skill assets without interpreting them. A
                # binary carrying credential-like bytes is withheld because it
                # cannot be redacted without corrupting the asset.
                raw = source.read_bytes()
                binary_view = raw.decode("latin-1", errors="ignore")
                secret_hits = sum(len(pattern.findall(binary_view)) for pattern in HIGH_CONFIDENCE_SECRETS)
                if secret_hits:
                    self.redaction_count += secret_hits
                    self.counts["binary_skill_files_withheld"] += 1
                    self.conflicts.append(
                        {
                            "path": f"skills/{relative}",
                            "source": str(source),
                            "reason": "credential-like bytes withheld; original remains in Hermes archive",
                        }
                    )
                    continue
                destination = self.workspace / "skills" / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                current_hash = digest_file(destination) if destination.is_file() else None
                previous_hash = self.previous_files.get(f"skills/{relative}")
                source_hash = digest_file(source)
                if current_hash and current_hash != source_hash and current_hash != previous_hash:
                    candidate = self._candidate_path(f"skills/{relative}")
                    candidate.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, candidate)
                    self.conflicts.append({"path": f"skills/{relative}", "source": str(source), "candidate": str(candidate)})
                    self.managed_files[f"skills/{relative}"] = current_hash
                    continue
                shutil.copy2(source, destination)
                os.chmod(destination, 0o600)
                self.managed_files[f"skills/{relative}"] = source_hash
                self.source_hashes[str(source)] = source_hash
                self.counts["skill_files"] += 1
                continue
            self.install_text(f"skills/{relative}", content, source=str(source))
            self.counts["skill_files"] += 1

    def import_legacy_curiosity(self) -> None:
        """Preserve prior sidecar state without trusting it as approved memory."""

        roots = [
            self.hermes_home / "curiosity",
            self.hermes_home / "memories" / "curiosity",
            self.hermes_home / "state" / "curiosity",
        ]
        target_root = self.state_dir / "mac-curiosity" / "hermes-import"
        for root in roots:
            if not root.is_dir():
                continue
            label = root.relative_to(self.hermes_home).as_posix().replace("/", "-")
            for source in sorted(root.rglob("*")):
                if not source.is_file() or source.is_symlink():
                    continue
                relative = Path(label) / source.relative_to(root)
                destination = target_root / relative
                raw = source.read_bytes()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    view = raw.decode("latin-1", errors="ignore")
                    if any(pattern.search(view) for pattern in HIGH_CONFIDENCE_SECRETS):
                        self.counts["curiosity_binary_files_withheld"] += 1
                        self.conflicts.append(
                            {
                                "path": f"state/mac-curiosity/hermes-import/{relative.as_posix()}",
                                "source": str(source),
                                "reason": "credential-like bytes withheld; original remains in Hermes archive",
                            }
                        )
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists() and digest_file(destination) != digest_file(source):
                        candidate = self._candidate_path(
                            f"state/mac-curiosity/{relative.as_posix()}"
                        )
                        candidate.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source, candidate)
                        self.conflicts.append(
                            {
                                "path": str(destination),
                                "source": str(source),
                                "candidate": str(candidate),
                            }
                        )
                        continue
                    shutil.copy2(source, destination)
                    os.chmod(destination, 0o600)
                else:
                    text, redactions = redact(text)
                    self.redaction_count += redactions
                    if (
                        destination.exists()
                        and destination.read_text(encoding="utf-8", errors="replace") != text
                    ):
                        candidate = self._candidate_path(
                            f"state/mac-curiosity/{relative.as_posix()}"
                        )
                        atomic_write(candidate, text)
                        self.conflicts.append(
                            {
                                "path": str(destination),
                                "source": str(source),
                                "candidate": str(candidate),
                            }
                        )
                        continue
                    atomic_write(destination, text)
                self.source_hashes[str(source)] = digest_file(source)
                self.counts["curiosity_files_preserved"] += 1

    def _recover_database(self, source: Path) -> Path | None:
        recovered = self.migration_dir / "recovered-state.db"
        sqlite_bin = shutil.which("sqlite3")
        if not sqlite_bin:
            return None
        temporary = recovered.with_suffix(".tmp.db")
        temporary.unlink(missing_ok=True)
        recover = subprocess.Popen(
            [sqlite_bin, str(source), ".recover"], stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        assert recover.stdout is not None
        restore = subprocess.run(
            [sqlite_bin, str(temporary)], stdin=recover.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        recover.stdout.close()
        recover_stderr = recover.stderr.read() if recover.stderr else b""
        recover_rc = recover.wait()
        if recover_rc or restore.returncode or not temporary.exists():
            temporary.unlink(missing_ok=True)
            atomic_write(
                self.migration_dir / "database-recovery-error.txt",
                "sqlite3 .recover failed without modifying the source database\n"
                f"recover_rc={recover_rc} restore_rc={restore.returncode}\n"
                + (recover_stderr + restore.stderr)[:4000].decode("utf-8", errors="replace"),
            )
            return None
        os.replace(temporary, recovered)
        os.chmod(recovered, 0o600)
        return recovered

    def _database(self) -> tuple[sqlite3.Connection | None, Path | None]:
        candidates = [
            self.hermes_home / "state.db",
            self.hermes_home / "data" / "state.db",
            self.hermes_home / "hermes.db",
        ]
        for source in candidates:
            if not source.is_file():
                continue
            try:
                conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
                conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
                return conn, source
            except sqlite3.DatabaseError:
                try:
                    conn.close()
                except Exception:
                    pass
                recovered = self._recover_database(source)
                if recovered:
                    conn = sqlite3.connect(f"file:{recovered}?mode=ro", uri=True)
                    conn.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
                    return conn, source
        return None, None

    @staticmethod
    def _day(value: Any) -> str:
        text = str(value or "").strip()
        match = re.match(r"(20\d\d-\d\d-\d\d)", text)
        return match.group(1) if match else "undated"

    def _messages(self, conn: sqlite3.Connection) -> Iterator[tuple[str, str, str, str]]:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(messages)")}
        needed = {"session_id", "role", "content", "timestamp"}
        if not needed <= columns:
            return
        query = "SELECT session_id, role, content, timestamp FROM messages ORDER BY timestamp, id"
        for session_id, role, content, timestamp in conn.execute(query):
            if content in (None, ""):
                continue
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, sort_keys=True)
            yield str(session_id or "unknown"), str(role or "unknown"), content, str(timestamp or "")

    def import_history(self) -> None:
        conn, source = self._database()
        if conn is None or source is None:
            self.counts["history_database_unavailable"] = 1
            return
        self.source_hashes[str(source)] = digest_file(source)
        history_root = self.workspace / "memory" / "hermes-history"
        previous_hash = (self.previous.get("trees") or {}).get("memory/hermes-history")
        current_hash = digest_tree(history_root) if history_root.exists() else None
        target_root = history_root
        conflict = bool(current_hash and previous_hash and current_hash != previous_hash)
        if conflict or (current_hash and not previous_hash):
            target_root = self._candidate_path("memory/hermes-history")
            self.conflicts.append(
                {
                    "path": "memory/hermes-history",
                    "source": str(source),
                    "preserved_hash": current_hash or "",
                    "candidate": str(target_root),
                }
            )
        staging = self.migration_dir / ".history-staging"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True, exist_ok=True)
        handles: dict[str, Any] = {}
        try:
            for session_id, role, content, timestamp in self._messages(conn):
                day = self._day(timestamp)
                handle = handles.get(day)
                if handle is None:
                    path = staging / f"{day}.md"
                    handle = path.open("w", encoding="utf-8")
                    handle.write(f"# Hermes conversation history — {day}\n\n")
                    handles[day] = handle
                content, redactions = redact(content)
                self.redaction_count += redactions
                handle.write(f"## {timestamp or 'timestamp unavailable'} · {role} · session {session_id}\n\n")
                handle.write(content.rstrip() + "\n\n")
                self.counts["history_messages"] += 1
        finally:
            for handle in handles.values():
                handle.close()
            conn.close()
        target_root.parent.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(target_root, ignore_errors=True)
        os.replace(staging, target_root)
        for path in target_root.rglob("*"):
            if path.is_file():
                os.chmod(path, 0o600)
        if target_root == history_root:
            self.managed_files.update(
                {
                    f"memory/hermes-history/{path.name}": digest_file(path)
                    for path in history_root.glob("*.md")
                }
            )

    def import_cron(self) -> None:
        source = self.hermes_home / "cron" / "jobs.json"
        raw = load_json(source, {})
        rows = raw.get("jobs") if isinstance(raw, dict) else raw
        if not isinstance(rows, list):
            rows = []
        if source.is_file():
            self.source_hashes[str(source)] = digest_file(source)
        jobs: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            schedule = row.get("schedule") if isinstance(row.get("schedule"), dict) else {}
            if schedule.get("kind") != "cron" or not schedule.get("expr"):
                continue
            prompt, redactions = redact(str(row.get("prompt") or ""))
            self.redaction_count += redactions
            jobs.append(
                {
                    "legacy_id": row.get("id"),
                    "name": str(row.get("name") or row.get("id") or "hermes-job"),
                    "cron": str(schedule["expr"]),
                    "message": prompt,
                    "enabled": bool(row.get("enabled", True)),
                    "delivery": row.get("deliver"),
                    "origin": row.get("origin"),
                    "legacy_script": row.get("script"),
                    "legacy_skill": row.get("skill"),
                }
            )
        plan = {
            "schema": "mac.openclaw_cron_migration.v1",
            "source": str(source),
            "generated_at": utcnow(),
            "jobs": jobs,
        }
        atomic_write(self.migration_dir / "cron-plan.json", json.dumps(plan, indent=2, sort_keys=True) + "\n")
        self.counts["cron_jobs"] = len(jobs)
        self.counts["cron_jobs_enabled"] = sum(1 for job in jobs if job["enabled"])

    def configured_workspace(self) -> bool:
        for name in ("SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"):
            text = read_text(self.workspace / name)
            if text and text.strip():
                return True
        return (self.workspace / "memory").is_dir()

    def run(self) -> dict[str, Any]:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.migration_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.workspace, 0o700)
        os.chmod(self.state_dir, 0o700)
        os.chmod(self.migration_dir, 0o700)
        hermes_exists = self.hermes_home.is_dir()
        if hermes_exists:
            self.import_identity()
            self.import_skills()
            self.import_legacy_curiosity()
            self.import_history()
            self.import_cron()
            mode = "hermes_import"
        elif self.configured_workspace():
            mode = "existing_openclaw"
        else:
            self.import_proposal()
            mode = "mentor_bootstrap"

        trees = {
            "memory/hermes-history": digest_tree(self.workspace / "memory" / "hermes-history"),
            "skills": digest_tree(self.workspace / "skills"),
        }
        manifest = {
            "schema": SCHEMA,
            "agent_id": self.agent_id,
            "mode": mode,
            "source_hermes_home": str(self.hermes_home) if hermes_exists else None,
            "workspace": str(self.workspace),
            "completed_at": utcnow(),
            "managed_files": dict(sorted(self.managed_files.items())),
            "trees": trees,
            "source_hashes": dict(sorted(self.source_hashes.items())),
            "counts": dict(sorted(self.counts.items())),
            "redactions": self.redaction_count,
            "conflicts": self.conflicts,
            "source_preserved": True,
        }
        atomic_write(self.manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        return manifest


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--hermes-home", type=Path, required=True)
    result.add_argument("--workspace", type=Path, required=True)
    result.add_argument("--state-dir", type=Path, required=True)
    result.add_argument("--migration-dir", type=Path, required=True)
    result.add_argument("--agent-id", required=True)
    result.add_argument("--public-identity", default="")
    result.add_argument("--identity-proposal", type=Path)
    result.add_argument("--report", type=Path)
    return result


def main(argv: Iterable[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        report = Importer(
            hermes_home=args.hermes_home.expanduser(),
            workspace=args.workspace.expanduser(),
            state_dir=args.state_dir.expanduser(),
            migration_dir=args.migration_dir.expanduser(),
            agent_id=args.agent_id,
            public_identity=args.public_identity,
            proposal=args.identity_proposal.expanduser() if args.identity_proposal else None,
        ).run()
    except Exception as exc:  # noqa: BLE001 - CLI must produce a durable failure report
        failure = {
            "schema": SCHEMA,
            "agent_id": args.agent_id,
            "status": "failed",
            "error": str(exc),
            "failed_at": utcnow(),
        }
        destination = args.report or (args.migration_dir / "last-run.json")
        atomic_write(destination.expanduser(), json.dumps(failure, indent=2, sort_keys=True) + "\n")
        print(json.dumps(failure, sort_keys=True))
        return 4
    report["status"] = "completed"
    destination = args.report or (args.migration_dir / "last-run.json")
    atomic_write(destination.expanduser(), json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
