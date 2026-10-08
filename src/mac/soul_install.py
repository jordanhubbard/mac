"""Install the soul graph into a host's Hermes: MCP server, seed and skill.

Hermes is where an agent talks to people, and it is the only MAC component that
loads MCP servers from a per-host config (``$HERMES_HOME/config.yaml``,
``mcp_servers:``). This module makes three idempotent changes there:

1. **MCP server.** An ``mcp_servers.soul`` entry that runs ``mac.soul_mcp``
   with the MAC venv's Python against ``$HERMES_HOME/soul.json``. Other
   ``mcp_servers`` entries (codegraph, say) are left exactly as they are.
2. **Seed.** When ``soul.json`` does not exist yet, build it from the agent's
   SOUL.md, USER.md and MEMORY.md (and Hermes' ``memories/`` copies). An
   existing ``soul.json`` is never touched: from then on the agent owns it.
3. **Skill.** ``$HERMES_HOME/skills/soul-graph/SKILL.md``, which tells the
   agent the tools exist and how to use them. It is only installed next to the
   MCP entry, so no agent is told about tools it does not have.

Coding agents (Claude Code, opencode) are deliberately not wired: they run in
an OpenShell sandbox that cannot see ``$HERMES_HOME``, and their memory of a
task belongs to the task, not to the agent.

``scripts/fleet-update`` runs this on every host after the code import check
and before services restart, so ``fleet-update --hermes`` both installs it and
restarts Hermes to load it. A host without a Hermes config is skipped.

Stdlib only, like ``mac.hermes_chat_config``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SERVER_NAME = "soul"
SKILL_NAME = "soul-graph"
MANAGED_MARKER = "managed by mac.soul_install"
SOUL_FILE = "soul.json"
# (relative path, source tag). SOUL.md is identity: its entries become axioms.
SOURCES: Tuple[Tuple[str, str], ...] = (
    ("SOUL.md", "soul"),
    ("USER.md", "user"),
    ("MEMORY.md", "memory"),
    ("memories/USER.md", "user"),
    ("memories/MEMORY.md", "memory"),
)
# Hermes separates memory entries with a section sign on its own line.
_ENTRY_SPLIT_RE = re.compile(r"\n\s*(?:\u00a7|---+)\s*\n|\n\s*\n")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def hermes_home(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from mac import mac_paths

    return mac_paths.hermes_home()


# --- 1. MCP server entry ----------------------------------------------------


def server_block(python: str, soul_file: Path) -> List[str]:
    return [
        "  %s:" % SERVER_NAME,
        "    command: %s" % json.dumps(python),
        "    args:",
        "      - -m",
        "      - mac.soul_mcp",
        "      - --soul-file",
        "      - %s" % json.dumps(str(soul_file)),
        "    timeout: 60",
        "    connect_timeout: 30",
        "    enabled: true",
    ]


def ensure_mcp_server(config: Path, python: str, soul_file: Path) -> str:
    """Add or refresh ``mcp_servers.soul`` in a Hermes config.yaml.

    Line-based, like ``hermes_chat_config.sync_config_yaml``, so comments and
    every other key survive. Returns ``added``, ``updated`` or ``unchanged``.
    """
    text = config.read_text(encoding="utf-8")
    lines = text.splitlines()
    block = server_block(python, soul_file)

    start = next((i for i, ln in enumerate(lines) if re.match(r"^mcp_servers:\s*$", ln)), None)
    if start is None:
        new_lines = lines + ["mcp_servers:"] + block
        state = "added"
    else:
        end = start + 1
        while end < len(lines) and (lines[end].startswith(" ") or not lines[end].strip()):
            end += 1
        section = lines[start + 1 : end]
        kept: List[str] = []
        existing: List[str] = []
        skipping = False
        for ln in section:
            if re.match(r"^  %s:\s*$" % re.escape(SERVER_NAME), ln):
                skipping = True
                existing.append(ln)
                continue
            if skipping and (ln.startswith("    ") or not ln.strip()):
                existing.append(ln)
                continue
            skipping = False
            kept.append(ln)
        if [ln for ln in existing if ln.strip()] == block:
            return "unchanged"
        state = "updated" if existing else "added"
        while kept and not kept[-1].strip():
            kept.pop()
        new_lines = lines[: start + 1] + block + kept + lines[end:]

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    shutil.copy2(config, config.with_name("%s.bak-%s-pre-soul" % (config.name, stamp)))
    tmp = config.with_name(config.name + ".soul-tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.chmod(tmp, config.stat().st_mode & 0o777)
    tmp.replace(config)
    return state


# --- 2. Seed ----------------------------------------------------------------


def _entries(text: str) -> List[Tuple[Optional[str], str]]:
    """Split markdown into (section heading, entry) pairs.

    An entry is a paragraph or a ``\u00a7``-separated memory item. A heading
    starts a section; the document title (the first ``#`` heading) does not.
    """
    out: List[Tuple[Optional[str], str]] = []
    section: Optional[str] = None
    seen_title = False
    for chunk in _ENTRY_SPLIT_RE.split(text.replace("\r\n", "\n")):
        body: List[str] = []
        for ln in chunk.strip().splitlines():
            m = _HEADING_RE.match(ln.strip())
            if m:
                if body:
                    out.append((section, "\n".join(body).strip()))
                    body = []
                if len(m.group(1)) == 1 and not seen_title:
                    seen_title = True
                    continue
                section = m.group(2)
                continue
            body.append(ln)
        entry = "\n".join(body).strip()
        if entry and entry not in ("\u00a7",) and not re.fullmatch(r"[-_*\s]+", entry):
            out.append((section, entry))
    return out


def seed_graph(home: Path, name: str = "soul"):
    """Build a SoulGraph from the agent's markdown memory files."""
    from mac.soul_graph import SoulGraph

    graph = SoulGraph(name=name)
    seen: set[str] = set()
    for relpath, source in SOURCES:
        path = home / relpath
        if not path.is_file():
            continue
        sections: Dict[str, str] = {}
        for heading, entry in _entries(path.read_text(encoding="utf-8", errors="replace")):
            if entry in seen:
                continue  # MEMORY.md and memories/MEMORY.md often overlap
            seen.add(entry)
            parents: List[str] = []
            if heading:
                key = "%s:%s" % (relpath, heading)
                if key not in sections:
                    sections[key] = graph.add(
                        heading,
                        tags={source, "section"},
                        pinned=(source == "soul"),
                        metadata={"source": relpath},
                    ).id
                parents = [sections[key]]
            graph.add(
                entry,
                tags={source},
                parents=parents,
                pinned=(source == "soul"),
                metadata={"source": relpath},
            )
    return graph


def ensure_seeded(home: Path, soul_file: Path) -> str:
    """Create soul.json from the markdown files if it does not exist yet.

    Returns ``exists`` (left alone), ``seeded:<n>`` or ``empty`` (no source
    files; the MCP server creates the file on first write).
    """
    if soul_file.exists():
        return "exists"
    graph = seed_graph(home)
    if not graph.nodes:
        return "empty"
    soul_file.parent.mkdir(parents=True, exist_ok=True)
    graph.save(soul_file)
    os.chmod(soul_file, 0o600)
    return "seeded:%d" % len(graph.nodes)


# --- 3. Skill ---------------------------------------------------------------


def skill_text() -> str:
    return (
        resources.files("mac")
        .joinpath("data", "hermes_skills", SKILL_NAME, "SKILL.md")
        .read_text(encoding="utf-8")
    )


def ensure_skill(home: Path) -> str:
    """Install the skill, replacing only a copy this module installed."""
    target = home / "skills" / SKILL_NAME / "SKILL.md"
    text = skill_text()
    if target.exists():
        current = target.read_text(encoding="utf-8", errors="replace")
        if current == text:
            return "unchanged"
        if MANAGED_MARKER not in current:
            return "skipped: a hand-written skill is already there"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return "installed"


# --- entry point ------------------------------------------------------------


def install(home: Path, python: str) -> Dict[str, str]:
    config = home / "config.yaml"
    if not config.is_file():
        return {"status": "skipped", "reason": "no Hermes config at %s" % config}
    soul_file = home / SOUL_FILE
    return {
        "status": "ok",
        "mcp_server": ensure_mcp_server(config, python, soul_file),
        "seed": ensure_seeded(home, soul_file),
        "skill": ensure_skill(home),
        "soul_file": str(soul_file),
    }


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hermes-home", help="default: $HERMES_HOME, else ~/.hermes")
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="interpreter Hermes runs the soul server with (default: this one)",
    )
    args = parser.parse_args(argv)
    try:
        result = install(hermes_home(args.hermes_home), args.python)
    except Exception as exc:  # noqa: BLE001 - reported, never half-silent
        print(json.dumps({"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
