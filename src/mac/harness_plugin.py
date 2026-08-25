"""Install mac's skills and MCP as an Agent Plugins 1.0 package (ADR 0023).

One canonical directory, thin per-harness pointers. The human CLI and the
agent plugin share the same ``mac admin mcp serve`` executable. This module
does not install ``mac`` itself.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from mac import __version__
from mac.mac_paths import plugin_dir as default_plugin_dir
from mac.mcp_server import server_command
from mac.models import ValidationError, json_dumps

RECEIPT_SCHEMA = "mac.plugin.receipt.v1"
PLUGIN_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"

HARNESSES = ("claude", "codex", "cursor", "opencode", "pi")

_MAC_SOURCE_MARKERS = (
    "src/mac/__init__.py",
    "skills/mac-cli/SKILL.md",
    "docs/adr/0023-one-skill-source-many-harness-plugins.md",
)


@dataclass(frozen=True)
class HarnessHomes:
    """Vendor config roots. Tests pass a fake user home; production uses Path.home()."""

    root: Path

    @property
    def claude(self) -> Path:
        return self.root / ".claude"

    @property
    def cursor(self) -> Path:
        return self.root / ".cursor"

    @property
    def codex(self) -> Path:
        return self.root / ".codex"

    @property
    def opencode(self) -> Path:
        return self.root / ".config" / "opencode"

    @property
    def agents(self) -> Path:
        return self.root / ".agents"

    @property
    def pi(self) -> Path:
        return self.root / ".pi"


def is_mac_source_tree(path: Path) -> bool:
    """True iff ``path`` is this repository, which the installer must refuse."""
    root = path.resolve()
    return all((root / marker).is_file() for marker in _MAC_SOURCE_MARKERS)


def find_skills_root(start: Optional[Path] = None) -> Path:
    """Locate the canonical ``skills/`` tree.

    Walks from ``start`` (cwd by default) then from this file's checkout
    layout. A wheel without a checkout fails closed with a named error.
    """
    candidates: List[Path] = []
    here = Path(start or Path.cwd()).resolve()
    candidates.append(here)
    candidates.extend(here.parents)
    candidates.append(Path(__file__).resolve().parents[2])
    seen: set[Path] = set()
    for base in candidates:
        if base in seen:
            continue
        seen.add(base)
        skills = base / "skills"
        if (skills / "mac-cli" / "SKILL.md").is_file():
            return skills
    raise ValidationError(
        "cannot find skills/mac-cli/SKILL.md; run mac admin plugin install "
        "from a mac checkout, or pass --skills-root"
    )


def detect_harnesses(homes: HarnessHomes) -> Dict[str, str]:
    """Map harness name to ``present`` or ``absent``. Presence is a home directory."""
    mapping = {
        "claude": homes.claude,
        "codex": homes.codex,
        "cursor": homes.cursor,
        "opencode": homes.opencode,
        "pi": homes.pi,
    }
    return {name: ("present" if path.is_dir() else "absent") for name, path in mapping.items()}


def _skill_dirs(skills_root: Path) -> List[Path]:
    return sorted(
        path for path in skills_root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
    )


def _write_canonical_plugin(plugin_root: Path, skills_root: Path) -> None:
    plugin_root.mkdir(parents=True, exist_ok=True)
    (plugin_root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": PLUGIN_SCHEMA,
                "name": "mac",
                "version": __version__,
                "description": "MAC control-plane skills and ledger MCP",
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    command = server_command()
    (plugin_root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": MCP_SCHEMA,
                "mcpServers": {
                    "mac": {
                        "type": "stdio",
                        "command": command[0],
                        "args": command[1:],
                    }
                },
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    dest_skills = plugin_root / "skills"
    if dest_skills.exists():
        shutil.rmtree(dest_skills)
    dest_skills.mkdir()
    for skill in _skill_dirs(skills_root):
        shutil.copytree(skill, dest_skills / skill.name, dirs_exist_ok=True)


def _symlink_skill(target_dir: Path, skill_src: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / skill_src.name
    if dest.is_symlink() or dest.exists():
        if dest.is_dir() and not dest.is_symlink():
            shutil.rmtree(dest)
        else:
            dest.unlink()
    dest.symlink_to(skill_src.resolve(), target_is_directory=True)


def _merge_mcp_json(path: Path, command: List[str]) -> None:
    document: Dict[str, Any] = {"mcpServers": {}}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            loaded = {}
        if isinstance(loaded, dict):
            document = loaded
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        servers = {}
        document["mcpServers"] = servers
    servers["mac"] = {
        "type": "stdio",
        "command": command[0],
        "args": command[1:],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _drop_mcp_server(path: Path) -> None:
    if not path.is_file():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    if not isinstance(document, dict):
        return
    servers = document.get("mcpServers")
    if isinstance(servers, dict):
        servers.pop("mac", None)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _wire_harness(
    name: str,
    homes: HarnessHomes,
    plugin_root: Path,
    *,
    detected: Mapping[str, str],
    repo: Optional[Path],
) -> str:
    if detected.get(name) != "present" and repo is None:
        return "skipped"
    skills_src = plugin_root / "skills"
    command = server_command()
    if name == "claude":
        for skill in _skill_dirs(skills_src):
            _symlink_skill(homes.claude / "skills", skill)
        _merge_mcp_json(homes.claude / "mcp.json", command)
        return "installed"
    if name == "cursor":
        if repo is not None:
            _merge_mcp_json(repo / ".cursor" / "mcp.json", command)
            return "installed"
        _merge_mcp_json(homes.cursor / "mcp.json", command)
        return "installed"
    if name == "codex":
        _merge_mcp_json(homes.codex / "mcp.json", command)
        return "installed"
    if name == "opencode":
        if repo is not None:
            for skill in _skill_dirs(skills_src):
                _symlink_skill(repo / ".agents" / "skills", skill)
            return "installed"
        for skill in _skill_dirs(skills_src):
            _symlink_skill(homes.opencode / "skills", skill)
            _symlink_skill(homes.agents / "skills", skill)
        _merge_mcp_json(homes.opencode / "mcp.json", command)
        return "installed"
    if name == "pi":
        for skill in _skill_dirs(skills_src):
            _symlink_skill(homes.pi / "skills", skill)
        return "installed"
    return "skipped"


def _unwire_harness(
    name: str, homes: HarnessHomes, plugin_root: Path, repo: Optional[Path]
) -> None:
    skill_names = (
        [path.name for path in _skill_dirs(plugin_root / "skills")]
        if (plugin_root / "skills").is_dir()
        else []
    )
    skill_homes = {
        "claude": [homes.claude / "skills"],
        "opencode": [homes.opencode / "skills", homes.agents / "skills"],
        "pi": [homes.pi / "skills"],
    }
    for directory in skill_homes.get(name, []):
        for skill_name in skill_names:
            dest = directory / skill_name
            if dest.is_symlink() or dest.exists():
                if dest.is_dir() and not dest.is_symlink():
                    continue
                dest.unlink(missing_ok=True)
    if name in {"claude", "cursor", "codex", "opencode"}:
        mcp_path = {
            "claude": homes.claude / "mcp.json",
            "cursor": homes.cursor / "mcp.json",
            "codex": homes.codex / "mcp.json",
            "opencode": homes.opencode / "mcp.json",
        }[name]
        _drop_mcp_server(mcp_path)
    if name == "cursor" and repo is not None:
        _drop_mcp_server(repo / ".cursor" / "mcp.json")
    if name == "opencode" and repo is not None:
        for skill_name in skill_names:
            dest = repo / ".agents" / "skills" / skill_name
            if dest.is_symlink():
                dest.unlink(missing_ok=True)


def _receipt_path(plugin_root: Path) -> Path:
    return plugin_root / "RECEIPT.json"


def install(
    *,
    scope: str = "global",
    repo: Optional[Path] = None,
    user_home: Optional[Path] = None,
    plugin_root: Optional[Path] = None,
    skills_root: Optional[Path] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Write the canonical plugin and wire detected (or repo-nominated) harnesses."""
    scope_value = str(scope or "global").strip().lower()
    if scope_value not in {"global", "repo"}:
        raise ValidationError("plugin install scope must be 'global' or 'repo'")
    repo_path = Path(repo).resolve() if repo is not None else None
    if scope_value == "repo":
        if repo_path is None:
            raise ValidationError("plugin install --repo is required for scope=repo")
        if is_mac_source_tree(repo_path):
            raise ValidationError(
                "refusing to install into the mac source tree; nominate another "
                "repository or use --scope global"
            )
    homes = HarnessHomes(Path(user_home).resolve() if user_home is not None else Path.home())
    root = Path(plugin_root).resolve() if plugin_root is not None else default_plugin_dir()
    source = Path(skills_root).resolve() if skills_root is not None else find_skills_root()
    _write_canonical_plugin(root, source)
    detected = detect_harnesses(homes)
    if scope_value == "repo":
        wired = {
            "claude": "skipped",
            "codex": "skipped",
            "cursor": _wire_harness(
                "cursor", homes, root, detected={"cursor": "present"}, repo=repo_path
            ),
            "opencode": _wire_harness(
                "opencode", homes, root, detected={"opencode": "present"}, repo=repo_path
            ),
            "pi": "skipped",
        }
    else:
        wired = {
            name: _wire_harness(name, homes, root, detected=detected, repo=None)
            for name in HARNESSES
        }
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "version": __version__,
        "scope": scope_value,
        "plugin_root": str(root),
        "skills_root": str(source),
        "repo": str(repo_path) if repo_path is not None else None,
        "harnesses": wired,
        "detected": detect_harnesses(homes),
        "installed_at": (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _receipt_path(root).write_text(json_dumps(receipt) + "\n", encoding="utf-8")
    return receipt


def status(*, plugin_root: Optional[Path] = None) -> Dict[str, Any]:
    root = Path(plugin_root).resolve() if plugin_root is not None else default_plugin_dir()
    path = _receipt_path(root)
    if not path.is_file():
        return {
            "schema": RECEIPT_SCHEMA,
            "installed": False,
            "plugin_root": str(root),
            "stale": False,
        }
    receipt = json.loads(path.read_text(encoding="utf-8"))
    stale = str(receipt.get("version") or "") != __version__
    receipt["installed"] = True
    receipt["stale"] = stale
    return receipt


def uninstall(
    *,
    plugin_root: Optional[Path] = None,
    user_home: Optional[Path] = None,
) -> Dict[str, Any]:
    root = Path(plugin_root).resolve() if plugin_root is not None else default_plugin_dir()
    path = _receipt_path(root)
    if not path.is_file():
        return {
            "schema": RECEIPT_SCHEMA,
            "installed": False,
            "removed": False,
            "plugin_root": str(root),
        }
    receipt = json.loads(path.read_text(encoding="utf-8"))
    homes = HarnessHomes(Path(user_home).resolve() if user_home is not None else Path.home())
    repo = Path(receipt["repo"]) if receipt.get("repo") else None
    for name in HARNESSES:
        _unwire_harness(name, homes, root, repo)
    shutil.rmtree(root, ignore_errors=True)
    return {
        "schema": RECEIPT_SCHEMA,
        "installed": False,
        "removed": True,
        "plugin_root": str(root),
        "harnesses": receipt.get("harnesses") or {},
    }


def peer_must_not_nudge() -> str:
    """Named obligation for tests: stall recovery is hub-only."""
    return "hub-only"
