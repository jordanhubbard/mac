"""Owning an agent from the command line.

The service layer can already record an owner, and the API has had `/humans`
for a while -- but nothing at the CLI could create a person or point an agent
at one, so the ownership model had no way to be used. These go through
`main()` for that reason: a gate that can only be reached from Python is a
gate the operator marking their own hardware cannot reach.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _agent(tmp_path, name="worker"):
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("%s-host" % name)
    return cp.register_agent(machine.id, name)


def test_a_person_can_be_registered(tmp_path):
    rc, out = _run(
        tmp_path, "admin", "human", "register", "jordanh", "--display-name", "Jordan Hubbard"
    )

    assert rc in (None, 0)
    assert out["username"] == "jordanh"


def test_an_agent_can_be_given_an_owner_and_made_private(tmp_path):
    """The whole point: hardware on someone's own network stops being
    advertised as fleet capacity."""
    agent = _agent(tmp_path, "rocky")
    _run(tmp_path, "admin", "human", "register", "jordanh")

    rc, out = _run(
        tmp_path, "agent", "update", agent.id, "--owner", "jordanh", "--visibility", "private"
    )

    assert rc in (None, 0)
    assert out["visibility"] == "private"
    assert out["owner_human_id"]


def test_an_agent_can_be_marked_shared(tmp_path):
    """The other half of the same operation: pooled workers stay everyone's."""
    agent = _agent(tmp_path, "pod")

    rc, out = _run(tmp_path, "agent", "update", agent.id, "--visibility", "shared")

    assert rc in (None, 0)
    assert out["visibility"] == "shared"


def test_making_an_agent_private_without_an_owner_is_refused(tmp_path):
    """Fail loudly rather than create capacity for nobody: a private agent with
    no owner matches no filer, so its queue would simply never move."""
    agent = _agent(tmp_path, "orphan")

    rc, _out = _run(tmp_path, "agent", "update", agent.id, "--visibility", "private")

    assert rc not in (None, 0)


def test_an_unknown_owner_is_refused(tmp_path):
    """Otherwise a typo stores a principal that resolves to nobody, and the
    agent silently stops taking work."""
    agent = _agent(tmp_path, "typo")

    rc, _out = _run(
        tmp_path,
        "agent",
        "update",
        agent.id,
        "--owner",
        "nobody-by-that-name",
        "--visibility",
        "private",
    )

    assert rc not in (None, 0)


def test_a_person_can_be_looked_up_by_username(tmp_path):
    """Ownership fields store ids; operators have usernames."""
    _run(tmp_path, "admin", "human", "register", "jordanh")

    rc, out = _run(tmp_path, "admin", "human", "show", "jordanh")

    assert rc in (None, 0)
    assert out["username"] == "jordanh"


def test_people_can_be_listed(tmp_path):
    """The lookup an operator needs before marking a node: which principals
    exist to be named as an owner."""
    _run(tmp_path, "admin", "human", "register", "jordanh")
    _run(tmp_path, "admin", "human", "register", "someone-else")

    rc, out = _run(tmp_path, "admin", "human", "list")

    assert rc in (None, 0)
    assert {"jordanh", "someone-else"} <= {row["username"] for row in out}


def test_preflight_answers_before_a_task_is_filed(tmp_path):
    """A task the fleet cannot satisfy does not fail -- it waits. On 2026-08-08
    one waited while eight idle agents watched, and litai blocks on a deadline,
    so the answer has to come before the task exists."""
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("preflight-host")
    cp.register_agent(machine.id, "worker", capabilities=["python"])

    rc, out = _run(tmp_path, "task", "preflight", "--capabilities", "python")

    assert rc in (None, 0)
    assert out["dispatchable"] is True


def test_preflight_names_a_host_fact_asked_for_as_a_capability(tmp_path):
    """The mapping that produces an unclaimable task. Reporting it as a missing
    skill is true and useless; the caller needs the form that works."""
    cp = control_plane_on(dsn_for(tmp_path))
    machine = cp.register_machine("preflight-host2")
    cp.register_agent(machine.id, "worker2", capabilities=["python"])

    rc, out = _run(tmp_path, "task", "preflight", "--capabilities", "linux")

    assert rc in (None, 0)
    assert out["dispatchable"] is False
    assert "required_hardware" in out["explanation"]
