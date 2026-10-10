"""The fleet inventory: one authoritative host list with explicit dispositions.

task_c817b785: fleet-update read ``~/.mac/fleet-hosts`` with hardcoded
fallbacks while AGENTS.md named ``~/.mac/fleets.yaml`` authoritative, and the
hub counted 40 agent rows (35 offline, mostly interactive sessions) as if they
were all workers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac import fleet_inventory
from mac.fleet_inventory import InventoryError, classify_agent
from mac.hermes_runtime import render_fleet_section
from mac.services import ControlPlane

INVENTORY = """\
version: 1
fleets:
  rocky:
    default: true
    hub_agent: rocky
    hub_url: http://hub:8789
    agents:
    - {name: natasha, target: jkh@10.0.0.1, os: linux}
    - {name: bullwinkle, target: jkh@10.0.0.2, os: linux, arch: x86_64, agent_id: agent_bw}
    - {name: rocky, target: jkh@10.0.0.3, os: darwin}
    - {name: gone, enabled: false, target: horde@gone, os: linux, notes: pod deleted}
    - {name: hgx-1, instance_kind: fungible, target: horde@hgx-1, os: linux}
    - {name: lab, lifecycle: external, managed_by: lab-team, target: lab@lab, os: linux}
  other:
    hub_url: http://other:8789
    hub_agent: other-hub
    agents:
    - {name: other-worker, target: horde@10.9.9.9, os: linux}
"""


def _write(tmp_path: Path, text: str = INVENTORY) -> Path:
    path = tmp_path / "fleets.yaml"
    path.write_text(text)
    return path


def test_every_entry_gets_an_explicit_disposition(tmp_path):
    inventory = fleet_inventory.load(_write(tmp_path))
    by_name = {entry.name: entry for entry in inventory.entries}

    assert inventory.fleet == "rocky"
    assert {name: entry.disposition for name, entry in by_name.items()} == {
        "natasha": "managed",
        "bullwinkle": "managed",
        "rocky": "managed",
        "gone": "retired",
        "hgx-1": "external",
        "lab": "external",
        "other-worker": "other_fleet",
    }
    assert by_name["rocky"].role == "hub"
    assert by_name["natasha"].agent_id == "agent_natasha"
    assert "agent_id" in by_name["natasha"].derived
    assert by_name["bullwinkle"].agent_id == "agent_bw"
    assert "agent_id" not in by_name["bullwinkle"].derived
    assert by_name["bullwinkle"].arch == "x86_64"
    assert by_name["gone"].reason == "pod deleted"
    assert by_name["hgx-1"].managed_by == fleet_inventory.FUNGIBLE_MANAGER
    assert by_name["lab"].managed_by == "lab-team"
    assert by_name["other-worker"].managed_by == "other"
    assert [entry.name for entry in inventory.updater_workers()] == ["natasha", "bullwinkle"]


def test_only_managed_hosts_resolve_for_the_updater(tmp_path):
    inventory = fleet_inventory.load(_write(tmp_path))
    assert inventory.resolve("natasha").target == "jkh@10.0.0.1"
    with pytest.raises(InventoryError, match="is retired"):
        inventory.resolve("gone")
    with pytest.raises(InventoryError, match="is external"):
        inventory.resolve("hgx-1")
    with pytest.raises(InventoryError, match="unknown host 'boris'"):
        inventory.resolve("boris")
    # Another fleet's host is not this fleet's to update.
    with pytest.raises(InventoryError, match="unknown host 'other-worker'"):
        inventory.resolve("other-worker")


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ("    - {name: natasha, target: jkh@10.0.0.7, os: linux}\n", "name natasha"),
        ("    - {name: twin, target: x@10.0.0.1, os: linux}\n", "ssh host 10.0.0.1"),
        (
            "    - {name: dup, target: x@10.0.0.8, agent_id: agent_natasha, os: linux}\n",
            "agent_id agent_natasha",
        ),
        ("    - {name: odd, target: x@10.0.0.9, role: gpu}\n", "role 'gpu'"),
        ("    - {name: odd, target: x@10.0.0.9, lifecycle: paused}\n", "lifecycle 'paused'"),
        ("    - {name: odd, target: x@10.0.0.9, lifecycle: external}\n", "needs managed_by"),
        ("    - {name: odd, os: linux}\n", "needs an ssh target"),
    ],
)
def test_conflicting_or_incomplete_identities_are_refused(tmp_path, extra, message):
    text = INVENTORY.replace("  other:\n", extra + "  other:\n")
    with pytest.raises(InventoryError, match=message):
        fleet_inventory.load(_write(tmp_path, text))


def test_a_missing_or_ambiguous_inventory_is_an_error(tmp_path):
    with pytest.raises(InventoryError, match="does not exist"):
        fleet_inventory.load(tmp_path / "absent.yaml")
    two_defaults = INVENTORY.replace("    hub_url: http://other:8789\n", "    default: true\n    hub_url: http://other:8789\n")
    with pytest.raises(InventoryError, match="several"):
        fleet_inventory.load(_write(tmp_path, two_defaults))
    assert fleet_inventory.load(_write(tmp_path, two_defaults), fleet="other").fleet == "other"


def test_cli_plan_and_updater_lines(tmp_path, capsys):
    path = _write(tmp_path)
    assert fleet_inventory.main(["--file", str(path), "workers"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "natasha jkh@10.0.0.1 agent_natasha",
        "bullwinkle jkh@10.0.0.2 agent_bw",
    ]
    assert fleet_inventory.main(["--file", str(path), "resolve", "rocky"]) == 0
    assert capsys.readouterr().out.split() == ["rocky", "jkh@10.0.0.3", "agent_rocky", "hub"]
    assert fleet_inventory.main(["--file", str(path), "plan"]) == 0
    plan = capsys.readouterr().out
    for name in ("natasha", "gone", "hgx-1", "lab", "other-worker"):
        assert name in plan
    assert fleet_inventory.main(["--file", str(path), "resolve", "gone"]) == 2
    assert "is retired" in capsys.readouterr().err


def test_classify_agent_separates_workers_from_sessions_and_services():
    classes = {"agent_natasha": "worker", "agent_rocky": "hub"}
    assert classify_agent("agent_natasha", "machine_natasha", classes) == "worker"
    assert classify_agent("agent_rocky", "machine_rocky", classes) == "hub"
    assert classify_agent("agent_operator", "machine_operator_persona", classes) == "virtual_service"
    assert classify_agent("agent_cli_1", "machine_cli_abc", classes) == "operator_session"
    assert classify_agent("agent_stray", "machine_stray", classes) == "unenrolled"


def test_operator_sessions_never_inflate_the_worker_count(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(_write(tmp_path)))
    cp = ControlPlane.in_memory()
    for name in ("natasha", "bullwinkle"):
        machine = cp.register_machine(name, machine_id="machine_%s" % name)
        cp.register_agent(machine.id, name, capabilities=["python"], agent_id="agent_%s" % name)
    session_machine = cp.register_machine("puck", machine_id="machine_cli_puck")
    for index in range(5):
        cp.register_agent(session_machine.id, "cli-session-%d" % index, agent_id="agent_cli_%d" % index)

    snapshot = cp.fleet_snapshot()

    classes = {member["name"]: member["agent_class"] for member in snapshot["members"]}
    assert classes["natasha"] == "worker"
    # bullwinkle is enrolled as agent_bw, so a row named agent_bullwinkle is not it.
    assert classes["bullwinkle"] == "unenrolled"
    assert classes["cli-session-0"] == "operator_session"
    assert snapshot["counts"]["worker"]["total"] == 1
    assert snapshot["counts"]["operator_session"]["total"] == 5
    assert "inventory_error" not in snapshot
    assert "_Workers: " in render_fleet_section(snapshot)
    assert "of 1 enrolled online._" in render_fleet_section(snapshot)


def test_snapshot_reports_an_unreadable_inventory_instead_of_guessing(tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_FLEETS_CONFIG", str(tmp_path / "absent.yaml"))
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("natasha", machine_id="machine_natasha")
    cp.register_agent(machine.id, "natasha", agent_id="agent_natasha")

    snapshot = cp.fleet_snapshot()

    assert "does not exist" in snapshot["inventory_error"]
    assert snapshot["members"][0]["agent_class"] == "unenrolled"
    assert snapshot["counts"]["worker"]["total"] == 0
