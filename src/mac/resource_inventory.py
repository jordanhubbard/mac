"""Normalization helpers for agent-reported runtime resources."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Dict


JsonDict = Dict[str, Any]


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        stripped = value.strip()
        return [stripped] if stripped else []
    if not isinstance(value, Iterable) or isinstance(value, dict):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def agent_resource_command_names(resources: JsonDict) -> set[str]:
    """Return every command name advertised by supported inventory shapes."""

    names: set[str] = set()
    for key in ("commands", "command_inventory"):
        inventory = resources.get(key)
        if isinstance(inventory, dict):
            names.update(_string_list(inventory.get("available")))
            commands = inventory.get("commands")
            if isinstance(commands, list):
                for item in commands:
                    if isinstance(item, str) and item.strip():
                        names.add(item.strip())
                    elif isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        if name:
                            names.add(name)
            paths = inventory.get("paths")
            if isinstance(paths, dict):
                names.update(str(name).strip() for name in paths if str(name).strip())
        elif isinstance(inventory, list):
            for item in inventory:
                if isinstance(item, str) and item.strip():
                    names.add(item.strip())
                elif isinstance(item, dict):
                    name = str(item.get("name") or "").strip()
                    if name:
                        names.add(name)
    return names
