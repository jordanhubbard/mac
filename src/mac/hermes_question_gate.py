"""Install the MAC question gate into a host's Hermes and refresh its runtime context.

MAC posts task questions to the Slack home channel, and a person answers in the
question's thread or with its code ("Q7 blue"). The MAC worker relays those
answers to the task board (see ``mac.worker``). Hermes answers everything in a
free-response channel, so every agent on the fleet used to reply to those
answers too. This module makes three idempotent changes under ``$HERMES_HOME``:

1. **Plugin.** ``plugins/mac-question-gate/``, a ``pre_gateway_dispatch`` hook
   that drops such answers before the agent sees them. A copy this module did
   not install is left alone.
2. **Enabled.** Hermes plugins are opt-in, so ``mac-question-gate`` is added to
   ``plugins.enabled`` in ``config.yaml`` (line-based, comments survive). A
   plugin someone listed under ``plugins.disabled`` stays disabled.
3. **Runtime context.** ``mac-runtime-context.{json,md}`` is regenerated with
   ``mac.hermes_runtime`` from the values in ``$HERMES_HOME/.env``, keeping the
   live Fleet and mood blocks, so runtime rules reach agents on every update
   rather than only when the gateway is reinstalled. If the regenerated identity,
   agent, environment or endpoints would differ from the current file, nothing is
   written: that is a reinstall, not a refresh.

``scripts/fleet-update`` runs this on every host next to ``mac.soul_install``;
Hermes loads the plugin and the context on restart (``fleet-update --hermes``).
A host without a Hermes config is skipped.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import time
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional

PLUGIN_NAME = "mac-question-gate"
PLUGIN_FILES = ("plugin.yaml", "__init__.py")
MANAGED_MARKER = "Managed by mac.hermes_question_gate"
# The context fields that say who this Hermes is; a refresh must not change them.
IDENTITY_KEYS = ("identity", "agent", "environment", "endpoints")


def hermes_home(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    from mac import mac_paths

    return mac_paths.hermes_home()


# --- 1. Plugin --------------------------------------------------------------


def plugin_source(name: str) -> str:
    return (
        resources.files("mac")
        .joinpath("data", "hermes_plugins", PLUGIN_NAME, name)
        .read_text(encoding="utf-8")
    )


def ensure_plugin(home: Path) -> str:
    """Install or update the plugin, replacing only a copy this module installed."""
    target = home / "plugins" / PLUGIN_NAME
    wanted = {name: plugin_source(name) for name in PLUGIN_FILES}
    current = {
        name: (target / name).read_text(encoding="utf-8", errors="replace")
        for name in PLUGIN_FILES
        if (target / name).is_file()
    }
    if current == wanted:
        return "unchanged"
    if target.exists() and MANAGED_MARKER not in current.get("__init__.py", ""):
        return "skipped: a plugin this module did not install is already there"
    target.mkdir(parents=True, exist_ok=True)
    for name, text in wanted.items():
        tmp = target / (name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(target / name)
    return "updated" if current else "installed"


# --- 2. plugins.enabled -----------------------------------------------------


def _section(lines: List[str], start: int) -> int:
    """The end (exclusive) of the indented block that starts after ``lines[start]``."""
    end = start + 1
    while end < len(lines) and (lines[end].startswith(" ") or not lines[end].strip()):
        end += 1
    return end


def _list_names(lines: List[str], key_line: int, end: int) -> List[str]:
    """Item names of the list at ``lines[key_line]`` (block or inline form)."""
    inline = re.match(r"^\s+\w+:\s*\[(.*)\]\s*$", lines[key_line])
    if inline:
        return [item.strip().strip("'\"") for item in inline.group(1).split(",") if item.strip()]
    names = []
    for ln in lines[key_line + 1 : end]:
        m = re.match(r"^\s*-\s*['\"]?([^'\"#\s]+)", ln)
        if not m:
            break
        names.append(m.group(1))
    return names


def ensure_enabled(config: Path) -> str:
    """Add the plugin to ``plugins.enabled``. Returns added, unchanged or disabled."""
    lines = config.read_text(encoding="utf-8").splitlines()
    start = next((i for i, ln in enumerate(lines) if re.match(r"^plugins:\s*$", ln)), None)
    if start is None:
        new_lines = lines + ["plugins:", "  enabled:", "    - %s" % PLUGIN_NAME]
    else:
        end = _section(lines, start)
        keys = {
            m.group(1): i
            for i in range(start + 1, end)
            for m in [re.match(r"^  (\w+):", lines[i])]
            if m
        }
        if "disabled" in keys and PLUGIN_NAME in _list_names(lines, keys["disabled"], end):
            return "disabled"
        if "enabled" not in keys:
            new_lines = lines[: start + 1] + ["  enabled:", "    - %s" % PLUGIN_NAME] + lines[start + 1 :]
        else:
            at = keys["enabled"]
            names = _list_names(lines, at, end)
            if PLUGIN_NAME in names:
                return "unchanged"
            if "[" in lines[at]:
                items = ["    - %s" % n for n in names + [PLUGIN_NAME]]
                new_lines = lines[:at] + ["  enabled:"] + items + lines[at + 1 :]
            else:
                last = at + len(names)
                indent = re.match(r"^(\s*)-", lines[last]).group(1) if names else "    "
                new_lines = lines[: last + 1] + ["%s- %s" % (indent, PLUGIN_NAME)] + lines[last + 1 :]

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    shutil.copy2(config, config.with_name("%s.bak-%s-pre-question-gate" % (config.name, stamp)))
    tmp = config.with_name(config.name + ".question-gate-tmp")
    tmp.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    os.chmod(tmp, config.stat().st_mode & 0o777)
    tmp.replace(config)
    return "added"


# --- 3. Runtime context -----------------------------------------------------


def read_env_file(path: Path) -> Dict[str, str]:
    values: Dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        values[key] = value.strip().strip("'\"")
    return values


def _blocks(text: str) -> List[tuple]:
    """The live Fleet and mood blocks in a runtime-context markdown, if present."""
    from mac.hermes_runtime import (
        FLEET_SECTION_BEGIN,
        FLEET_SECTION_END,
        MOOD_SECTION_BEGIN,
        MOOD_SECTION_END,
        refresh_fleet_section,
        refresh_mood_section,
    )

    found = []
    for begin, end, refresh in (
        (FLEET_SECTION_BEGIN, FLEET_SECTION_END, refresh_fleet_section),
        (MOOD_SECTION_BEGIN, MOOD_SECTION_END, refresh_mood_section),
    ):
        if begin in text and end in text:
            found.append((refresh, begin + text.split(begin, 1)[1].split(end, 1)[0] + end))
    return found


def refresh_runtime_context(home: Path) -> str:
    """Regenerate the runtime context in place; see the module docstring."""
    from mac import hermes_runtime, mac_paths

    env = read_env_file(home / ".env")
    json_path = Path(env.get("MAC_HERMES_RUNTIME_CONTEXT_FILE") or home / "mac-runtime-context.json")
    md_path = Path(env.get("MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN") or home / "mac-runtime-context.md")
    if not json_path.is_file():
        return "skipped: no runtime context at %s" % json_path
    old = json.loads(json_path.read_text(encoding="utf-8"))
    workspace = env.get("MAC_HERMES_WORKSPACE") or env.get("SRC_DIR")
    kwargs = dict(
        agent_name=env.get("AGENT") or env.get("MAC_WORKER_AGENT_NAME") or "agent",
        fleet_name=env.get("FLEET_NAME") or "mac",
        mac_url=env.get("MAC_HUB_URL") or env.get("MAC_URL") or "",
        hermes_home=Path(env.get("HERMES_HOME") or home),
        mac_home=Path(env.get("MAC_HOME") or mac_paths.mac_home()),
        tenant_id=env.get("MAC_FLEET_TENANT_ID"),
        persona_id=env.get("MAC_HERMES_PERSONA_ID"),
        hermes_instance_id=env.get("MAC_HERMES_INSTANCE_ID"),
        agent_id=env.get("MAC_AGENT_ID"),
        workspace_path=Path(workspace) if workspace else None,
    )
    new = hermes_runtime.build_runtime_context(**kwargs)
    changed = [key for key in IDENTITY_KEYS if old.get(key) != new.get(key)]
    if changed:
        return "skipped: refreshing would change %s; reinstall the gateway instead" % ", ".join(changed)
    stale = {k: v for k, v in old.items() if k != "generated_at"}
    if stale == {k: v for k, v in new.items() if k != "generated_at"}:
        return "unchanged"
    blocks = _blocks(md_path.read_text(encoding="utf-8", errors="replace")) if md_path.is_file() else []
    hermes_runtime.write_runtime_context(
        context_path=json_path, markdown_path=md_path, hermes_env_path=home / ".env", **kwargs
    )
    for refresh, block in blocks:
        refresh(md_path, block)
    return "refreshed"


# --- entry point ------------------------------------------------------------


def install(home: Path) -> Dict[str, str]:
    config = home / "config.yaml"
    if not config.is_file():
        return {"status": "skipped", "reason": "no Hermes config at %s" % config}
    result = {"status": "ok", "plugin": ensure_plugin(home)}
    result["enabled"] = ensure_enabled(config) if not result["plugin"].startswith("skipped") else "skipped"
    result["runtime_context"] = refresh_runtime_context(home)
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hermes-home", help="default: $HERMES_HOME, else ~/.hermes")
    args = parser.parse_args(argv)
    try:
        result = install(hermes_home(args.hermes_home))
    except Exception as exc:  # noqa: BLE001 - reported, never half-silent
        print(json.dumps({"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
