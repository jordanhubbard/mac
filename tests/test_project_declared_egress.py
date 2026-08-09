"""Sandbox egress is declared on the PROJECT and projected onto assignments.

ADR 0009 §2a's goal was not "a repo can declare egress" — it was that a repo's
hosts stop being carried by every sandbox in the fleet. The declaration
therefore has to live somewhere a whole repository's worth of tasks can inherit
it from, and be applied to tasks that already exist.

Two properties matter and are pinned here:

* **Ownership.** A task's own ``metadata.egress_contract`` is written by whoever
  created the task — any hub credential. Project metadata is operator policy.
  The assignment projection strips the former and applies the latter, so the
  ``hub_declared`` trust tier means what its name says.
* **Reach.** The projection happens at claim time, so a declaration added today
  applies to the hundreds of tasks created before it.
"""

from __future__ import annotations

import pytest

from mac.models import json_dumps
from mac.services import ControlPlane

HOSTS = ["opensky-network.org", "aviationweather.gov", "tfr.faa.gov"]


def _project(cp: ControlPlane, name: str, block) -> None:
    cp.store.execute(
        "INSERT INTO projects (id, name, description, metadata, status, "
        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "project_%s" % name.lower(),
            name,
            "",
            json_dumps({"egress_contract": block} if block is not None else {}),
            "active",
            "t",
            "t",
        ),
    )


@pytest.fixture
def fleet():
    cp = ControlPlane.in_memory()
    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "worker-1", capabilities=["python"])
    return cp, agent


def _assignment_egress(cp, agent, task):
    cp.claim_task(task.id, agent.id)
    assignment = cp._active_assignment_for_agent(cp.get_agent(agent.id))
    return (assignment or {}).get("task", {}).get("metadata", {}).get(
        "egress_contract"
    )


# --- the declaration reaches the sandbox ----------------------------------


def test_project_declaration_is_projected_onto_the_assignment(fleet):
    cp, agent = fleet
    _project(cp, "Aviation", {"hosts": HOSTS, "reason": "integration data"})
    task = cp.create_task("fly", project="Aviation", required_capabilities=["python"])

    projected = _assignment_egress(cp, agent, task)
    assert projected["hosts"] == HOSTS
    assert projected["reason"] == "integration data"
    assert projected["source"] == "project"


def test_it_reaches_tasks_created_before_the_declaration_existed(fleet):
    """The reason this is projected at claim time rather than stamped at task
    creation: Aviation had hundreds of tasks before anyone declared its hosts."""
    cp, agent = fleet
    _project(cp, "Aviation", None)
    task = cp.create_task("old", project="Aviation", required_capabilities=["python"])

    cp.store.execute(
        "UPDATE projects SET metadata = ? WHERE name = ?",
        (json_dumps({"egress_contract": {"hosts": HOSTS}}), "Aviation"),
    )
    assert _assignment_egress(cp, agent, task)["hosts"] == HOSTS


def test_a_project_with_no_declaration_grants_nothing(fleet):
    cp, agent = fleet
    _project(cp, "Quiet", None)
    task = cp.create_task("x", project="Quiet", required_capabilities=["python"])
    assert _assignment_egress(cp, agent, task) is None


def test_an_unregistered_project_grants_nothing(fleet):
    cp, agent = fleet
    task = cp.create_task("x", project="ghost", required_capabilities=["python"])
    assert _assignment_egress(cp, agent, task) is None


# --- ownership: a task cannot declare its own -----------------------------


def test_a_task_cannot_smuggle_its_own_egress_contract(fleet):
    """Previously any task author could declare egress. Stripping it here is
    what makes `hub_declared` mean "an operator approved this"."""
    cp, agent = fleet
    _project(cp, "Aviation", {"hosts": ["opensky-network.org"]})
    task = cp.create_task(
        "sneaky",
        project="Aviation",
        required_capabilities=["python"],
        metadata={"egress_contract": {"hosts": ["attacker.example"]}},
    )

    projected = _assignment_egress(cp, agent, task)
    assert projected["hosts"] == ["opensky-network.org"]
    assert "attacker.example" not in str(projected)


def test_a_task_contract_is_stripped_even_with_no_project_declaration(fleet):
    """Strip unconditionally, like break_glass_authorization — otherwise the
    smuggled value survives for exactly the projects that declared nothing."""
    cp, agent = fleet
    _project(cp, "Quiet", None)
    task = cp.create_task(
        "sneaky",
        project="Quiet",
        required_capabilities=["python"],
        metadata={"egress_contract": {"hosts": ["attacker.example"]}},
    )
    assert _assignment_egress(cp, agent, task) is None


def test_the_durable_task_row_is_left_alone(fleet):
    """The projection is transient; it must not rewrite stored task metadata."""
    cp, agent = fleet
    _project(cp, "Aviation", {"hosts": HOSTS})
    task = cp.create_task(
        "fly",
        project="Aviation",
        required_capabilities=["python"],
        metadata={"egress_contract": {"hosts": ["attacker.example"]}},
    )
    _assignment_egress(cp, agent, task)
    stored = cp.get_task(task.id).metadata.get("egress_contract")
    assert stored == {"hosts": ["attacker.example"]}


# --- hosts are re-validated on the way out --------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        "evil.example\n  attacker_block:\n    name: pwned",
        "**.evil.example",
        "https://evil.example",
        "evil.example:443",
        "localhost",
        "10.0.0.1",
        "",
        None,
        123,
    ],
)
def test_malformed_hosts_are_dropped_not_rendered(fleet, bad):
    """Policy YAML is built by concatenation, so a malformed host is an
    injection vector however it reached the database."""
    cp, agent = fleet
    _project(cp, "Aviation", {"hosts": ["opensky-network.org", bad]})
    task = cp.create_task("fly", project="Aviation", required_capabilities=["python"])

    projected = _assignment_egress(cp, agent, task)
    assert projected["hosts"] == ["opensky-network.org"]


def test_all_malformed_means_no_contract_at_all(fleet):
    cp, agent = fleet
    _project(cp, "Aviation", {"hosts": ["**.evil.example"]})
    task = cp.create_task("fly", project="Aviation", required_capabilities=["python"])
    assert _assignment_egress(cp, agent, task) is None


def test_duplicates_are_collapsed(fleet):
    cp, agent = fleet
    _project(cp, "Aviation", {"hosts": ["tfr.faa.gov", "TFR.FAA.GOV", "tfr.faa.gov."]})
    task = cp.create_task("fly", project="Aviation", required_capabilities=["python"])
    assert _assignment_egress(cp, agent, task)["hosts"] == ["tfr.faa.gov"]


def test_a_non_list_hosts_value_is_ignored(fleet):
    cp, agent = fleet
    _project(cp, "Aviation", {"hosts": "opensky-network.org"})
    task = cp.create_task("fly", project="Aviation", required_capabilities=["python"])
    assert _assignment_egress(cp, agent, task) is None


# --- the fleet-wide block is gone -----------------------------------------


def test_the_operator_template_no_longer_declares_aviation_hosts():
    """The point of the exercise: those hosts stop being carried by every
    sandbox in the fleet, including repos with no business reaching them."""
    yaml = pytest.importorskip("yaml")
    from pathlib import Path

    template = (
        Path(__file__).resolve().parents[1]
        / "deploy"
        / "openshell"
        / "mac-hermes-policy.yaml"
    )
    text = template.read_text(encoding="utf-8")
    parsed = yaml.safe_load(
        text.replace("__MAC_HUB_HOST__", "h").replace("__MAC_HUB_PORT__", "8789")
        .replace("__MODEL_GATEWAY_HOST__", "g").replace("__RUNTIME_PY__", "/p")
        .replace("__RUNTIME_VENV__", "/v").replace("__RUNTIME_SRC__", "/s")
        .replace("__CACHE_DIR__", "/c").replace("__CONFIG_DIR__", "/cfg")
        .replace("__AGENT_USER__", "u")
    )
    assert "aviation_apis" not in (parsed.get("network_policies") or {})
    for host in HOSTS:
        assert host not in str(parsed.get("network_policies") or {}), host
