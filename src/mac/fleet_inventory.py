"""The fleet inventory: one authoritative list of enrolled hosts.

``~/.mac/fleets.yaml`` is the inventory (``mac_paths.fleets_config``). Every
consumer that asks "which hosts are ours?" reads it through this module: the
updater (``scripts/fleet-update``), the ``mac admin fleet inventory`` plan, and
the hub's fleet projection. There is no second host list.

Each entry resolves to an explicit identity and disposition:

- ``name``, ``fleet``, ``target`` (ssh), ``agent_id`` (the hub agent row);
- ``role``: ``hub`` (the control plane host, updated as the hub) or ``worker``;
- ``os`` and ``arch`` (``arch`` is ``""`` until recorded);
- ``lifecycle``: ``managed`` (this fleet's updater owns it), ``external`` (a
  named other mechanism owns it, ``managed_by``), or ``retired``.

Fields may be written explicitly in the YAML (``agent_id``, ``role``, ``arch``,
``lifecycle``, ``managed_by``). Where one is absent it is derived from facts the
file already records (``enabled: false`` is retired, ``instance_kind:
fungible`` is owned by the fungible provisioner, the fleet's ``hub_agent`` is
the hub), and the derivation is reported in ``derived`` so nothing is silently
assumed. Every entry in every fleet gets a disposition; entries in other fleets
are reported as ``other_fleet`` rather than omitted.

The topology lives outside Git, in the operator's home; this module holds only
the rules.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

SCHEMA = "mac.fleet_inventory.v1"
ROLES = ("hub", "worker")
LIFECYCLES = ("managed", "external", "retired")
FUNGIBLE_MANAGER = "fungible-provisioner"

#: Hub agent classes (see ``classify_agent``). Only ``worker`` is execution
#: capacity; the rest are visible but never counted as workers.
AGENT_CLASSES = ("worker", "hub", "virtual_service", "operator_session", "unenrolled")


class InventoryError(ValueError):
    """The inventory is unreadable or inconsistent; nothing may act on it."""


@dataclass(frozen=True)
class Entry:
    name: str
    fleet: str
    target: str
    agent_id: str
    role: str
    os: str
    arch: str
    lifecycle: str
    managed_by: str
    disposition: str
    reason: str = ""
    derived: List[str] = field(default_factory=list)

    @property
    def ssh_host(self) -> str:
        return self.target.rsplit("@", 1)[-1]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Inventory:
    path: str
    fleet: str
    hub_url: str
    entries: List[Entry]

    def managed(self, role: Optional[str] = None) -> List[Entry]:
        return [
            entry
            for entry in self.entries
            if entry.disposition == "managed" and (role is None or entry.role == role)
        ]

    def updater_workers(self) -> List[Entry]:
        """Workers this fleet's updater owns, in file order."""
        return self.managed("worker")

    def resolve(self, name: str) -> Entry:
        """The managed entry the updater may act on, or ``InventoryError``."""
        for entry in self.entries:
            if entry.fleet == self.fleet and entry.name == name:
                if entry.disposition != "managed":
                    raise InventoryError(
                        "host '%s' is %s%s; fleet-update only acts on managed hosts"
                        % (name, entry.disposition, (" (%s)" % entry.reason) if entry.reason else "")
                    )
                return entry
        raise InventoryError(
            "unknown host '%s'; enroll it in %s under fleet '%s'" % (name, self.path, self.fleet)
        )

    def agent_classes(self) -> Dict[str, str]:
        """agent_id -> class, for every agent identity the inventory names."""
        classes: Dict[str, str] = {}
        for entry in self.entries:
            if entry.fleet == self.fleet:
                classes[entry.agent_id] = "hub" if entry.role == "hub" else "worker"
        return classes

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "path": self.path,
            "fleet": self.fleet,
            "hub_url": self.hub_url,
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _load_yaml(path: Path) -> Dict[str, Any]:
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - pyyaml is a core dependency
        raise InventoryError("pyyaml is required to read %s" % path) from exc
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise InventoryError("fleet inventory %s does not exist" % path) from exc
    except OSError as exc:
        raise InventoryError("cannot read fleet inventory %s: %s" % (path, exc)) from exc
    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise InventoryError("fleet inventory %s is not valid YAML: %s" % (path, exc)) from exc
    if not isinstance(data, dict) or not isinstance(data.get("fleets"), dict):
        raise InventoryError("fleet inventory %s has no 'fleets' mapping" % path)
    return data


def _select_fleet(fleets: Dict[str, Any], wanted: Optional[str]) -> str:
    if wanted:
        if wanted not in fleets:
            raise InventoryError("fleet '%s' is not in the inventory" % wanted)
        return wanted
    defaults = [name for name, body in fleets.items() if isinstance(body, dict) and body.get("default")]
    if len(defaults) == 1:
        return defaults[0]
    if len(fleets) == 1:
        return next(iter(fleets))
    raise InventoryError(
        "the inventory names %s default fleets; mark exactly one 'default: true' or pass --fleet"
        % ("no" if not defaults else "several (%s)" % ", ".join(defaults))
    )


def _entry(fleet_name: str, fleet: Dict[str, Any], raw: Dict[str, Any], selected: str) -> Entry:
    name = str(raw.get("name") or "").strip()
    if not name:
        raise InventoryError("fleet '%s' has an agent entry with no name" % fleet_name)
    derived: List[str] = []
    agent_id = str(raw.get("agent_id") or "").strip()
    if not agent_id:
        agent_id = "agent_%s" % name
        derived.append("agent_id")
    role = str(raw.get("role") or "").strip()
    if not role:
        role = "hub" if name == str(fleet.get("hub_agent") or "") else "worker"
        derived.append("role")
    if role not in ROLES:
        raise InventoryError("%s/%s: role '%s' is not one of %s" % (fleet_name, name, role, ROLES))
    notes = " ".join(str(raw.get("notes") or "").split())
    lifecycle = str(raw.get("lifecycle") or "").strip()
    managed_by = str(raw.get("managed_by") or "").strip()
    reason = ""
    if not lifecycle:
        derived.append("lifecycle")
        if raw.get("enabled", True) is False:
            lifecycle, reason = "retired", notes or "enabled: false"
        elif str(raw.get("instance_kind") or "") == "fungible":
            lifecycle = "external"
            managed_by = managed_by or FUNGIBLE_MANAGER
            reason = "instance_kind: fungible"
        else:
            lifecycle = "managed"
    elif lifecycle == "retired":
        reason = notes or "lifecycle: retired"
    if lifecycle not in LIFECYCLES:
        raise InventoryError(
            "%s/%s: lifecycle '%s' is not one of %s" % (fleet_name, name, lifecycle, LIFECYCLES)
        )
    if lifecycle == "external" and not managed_by:
        raise InventoryError("%s/%s: lifecycle external needs managed_by" % (fleet_name, name))
    if fleet_name != selected:
        disposition = "other_fleet"
        managed_by = managed_by or fleet_name
        reason = reason or "enrolled in fleet '%s'" % fleet_name
    else:
        disposition = lifecycle
    if lifecycle == "external" and fleet_name == selected:
        reason = reason or "owned by %s" % managed_by
    target = str(raw.get("target") or "").strip()
    if disposition == "managed" and not target:
        raise InventoryError("%s/%s: a managed host needs an ssh target" % (fleet_name, name))
    return Entry(
        name=name,
        fleet=fleet_name,
        target=target,
        agent_id=agent_id,
        role=role,
        os=str(raw.get("os") or "").strip(),
        arch=str(raw.get("arch") or "").strip(),
        lifecycle=lifecycle,
        managed_by=managed_by,
        disposition=disposition,
        reason=reason,
        derived=derived,
    )


def _check_conflicts(entries: Iterable[Entry]) -> None:
    problems: List[str] = []
    seen: Dict[tuple, str] = {}
    for entry in entries:
        keys = [("name", entry.fleet, entry.name)]
        if entry.disposition in ("managed", "external"):
            keys.append(("agent_id", entry.agent_id))
        if entry.disposition == "managed":
            keys.append(("ssh host", entry.ssh_host))
        owner = "%s/%s" % (entry.fleet, entry.name)
        for key in keys:
            if key in seen:
                problems.append("%s %s is claimed by %s and %s" % (key[0], key[-1], seen[key], owner))
            else:
                seen[key] = owner
    hubs = [e for e in entries if e.disposition == "managed" and e.role == "hub"]
    if len({e.fleet for e in hubs}) != len(hubs):
        problems.append("more than one managed hub: %s" % ", ".join(e.name for e in hubs))
    if problems:
        raise InventoryError("conflicting inventory identities: " + "; ".join(problems))


def load(path: Optional[Path] = None, fleet: Optional[str] = None) -> Inventory:
    """Read and validate the inventory. Raises ``InventoryError`` on any doubt."""
    if path is None:
        from mac import mac_paths

        path = mac_paths.fleets_config()
    path = Path(path).expanduser()
    data = _load_yaml(path)
    fleets = data["fleets"]
    selected = _select_fleet(fleets, fleet)
    entries: List[Entry] = []
    for fleet_name, body in fleets.items():
        if not isinstance(body, dict):
            raise InventoryError("fleet '%s' is not a mapping" % fleet_name)
        agents = body.get("agents") or []
        if not isinstance(agents, list):
            raise InventoryError("fleet '%s' agents is not a list" % fleet_name)
        for raw in agents:
            if not isinstance(raw, dict):
                raise InventoryError("fleet '%s' has a non-mapping agent entry" % fleet_name)
            entries.append(_entry(fleet_name, body, raw, selected))
    _check_conflicts(entries)
    return Inventory(
        path=str(path),
        fleet=selected,
        hub_url=str(fleets[selected].get("hub_url") or ""),
        entries=entries,
    )


def classify_agent(agent_id: str, machine_id: str, classes: Dict[str, str]) -> str:
    """What a hub agent row is, so operator sessions never count as workers.

    The inventory decides workers and the hub; the hub's own virtual services
    (operator persona, reviewer) run on ``machine_operator_*``; interactive
    sessions register from ``machine_cli_*``. Anything else heartbeating is
    ``unenrolled``: visible, but not execution capacity until enrolled.
    """
    if agent_id in classes:
        return classes[agent_id]
    machine = machine_id or ""
    if machine.startswith("machine_operator_"):
        return "virtual_service"
    if machine.startswith("machine_cli_"):
        return "operator_session"
    return "unenrolled"


def _print_plan(inventory: Inventory) -> None:
    print("fleet inventory %s (fleet %s, hub %s)" % (inventory.path, inventory.fleet, inventory.hub_url))
    for entry in inventory.entries:
        detail = entry.reason or entry.managed_by
        print(
            "  %-24s %-11s %-6s %-6s %-26s %s%s%s"
            % (
                entry.name,
                entry.disposition,
                entry.role,
                entry.os or "-",
                entry.agent_id,
                entry.target or "-",
                ("  [%s]" % detail) if detail and entry.disposition != "managed" else "",
                ("  (derived: %s)" % ", ".join(entry.derived)) if entry.derived else "",
            )
        )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m mac.fleet_inventory", description=__doc__.split("\n")[0])
    parser.add_argument("--file", type=Path, help="inventory path (default: mac_paths.fleets_config())")
    parser.add_argument("--fleet", help="fleet name (default: the one marked default)")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("plan", help="every entry with its disposition (human-readable)")
    sub.add_parser("json", help="the whole inventory as JSON")
    sub.add_parser("workers", help="managed workers: '<name> <ssh_target> <agent_id>' per line")
    sub.add_parser("hub", help="the managed hub: '<name> <ssh_target> <agent_id>'")
    resolve = sub.add_parser("resolve", help="one managed host: '<name> <ssh_target> <agent_id> <role>'")
    resolve.add_argument("name")
    args = parser.parse_args(argv)
    try:
        inventory = load(args.file, args.fleet)
        if args.cmd == "plan":
            _print_plan(inventory)
        elif args.cmd == "json":
            print(json.dumps(inventory.to_dict(), indent=2, sort_keys=True))
        elif args.cmd == "workers":
            for entry in inventory.updater_workers():
                print(entry.name, entry.target, entry.agent_id)
        elif args.cmd == "hub":
            hubs = [e for e in inventory.managed("hub") if e.fleet == inventory.fleet]
            if len(hubs) != 1:
                raise InventoryError("fleet '%s' has %d managed hubs" % (inventory.fleet, len(hubs)))
            print(hubs[0].name, hubs[0].target, hubs[0].agent_id)
        else:
            entry = inventory.resolve(args.name)
            print(entry.name, entry.target, entry.agent_id, entry.role)
    except InventoryError as exc:
        print("fleet inventory: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
