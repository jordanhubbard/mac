"""CLI coverage for `mac project egress` — project-declared sandbox egress.

The hosts a project declares here are granted to its tasks' sandboxes at the
``hub_declared`` trust tier, so the CLI is a security surface: what it writes,
and where it writes it, decides what an agent running ``--yolo`` can reach.

Two properties are asserted rather than left to a reader's care:

* a malformed host is refused **at the CLI**, because policy YAML is assembled
  by concatenation and a glob, port or newline is an injection vector; and
* the value lands on the PROJECT, so it reaches every task in that project —
  including the ones created before the declaration, which is the whole reason
  it is not declared per task.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for


def _run(db, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(db), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    if not raw:
        return rc, None
    try:
        return rc, json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return rc, raw


@pytest.fixture
def project(tmp_path):
    cp = control_plane_on(dsn_for(tmp_path))
    cp.create_project("Aviation", description="flight suite")
    return tmp_path


def test_grant_list_revoke_round_trip(project):
    rc, _ = _run(
        project,
        "project",
        "egress",
        "grant",
        "Aviation",
        "opensky-network.org",
        "--reason",
        "ADS-B state vectors",
    )
    assert rc == 0

    rc, listed = _run(project, "project", "egress", "list", "Aviation")
    assert rc == 0
    assert listed["hosts"] == ["opensky-network.org"]
    assert listed["reason"] == "ADS-B state vectors"
    # Named in the output so an operator can see the tier without reading code.
    assert listed["trust_tier"] == "hub_declared"

    rc, _ = _run(project, "project", "egress", "grant", "Aviation", "tfr.faa.gov")
    rc, listed = _run(project, "project", "egress", "list", "Aviation")
    assert listed["hosts"] == ["opensky-network.org", "tfr.faa.gov"]

    rc, _ = _run(project, "project", "egress", "revoke", "Aviation", "tfr.faa.gov")
    rc, listed = _run(project, "project", "egress", "list", "Aviation")
    assert listed["hosts"] == ["opensky-network.org"]


def test_a_project_with_no_declaration_lists_nothing(project):
    rc, listed = _run(project, "project", "egress", "list", "Aviation")
    assert rc == 0
    assert listed["hosts"] == []


@pytest.mark.parametrize(
    "bad",
    [
        "**.evil.example",
        "https://evil.example",
        "evil.example:443",
        "evil.example\n  attacker: x",
        "localhost",
        "10.0.0.1",
    ],
)
def test_a_malformed_host_is_refused_at_the_cli(project, bad):
    """Refused here rather than in the renderer: the operator finds out at the
    point of the mistake, and a bad value never reaches the database."""
    with pytest.raises(SystemExit):
        _run(project, "project", "egress", "grant", "Aviation", bad)

    rc, listed = _run(project, "project", "egress", "list", "Aviation")
    assert listed["hosts"] == []


def test_granting_an_unknown_project_fails_loudly(project):
    with pytest.raises(SystemExit):
        _run(project, "project", "egress", "grant", "ghost", "opensky-network.org")


def test_the_declaration_lands_on_the_project_not_a_task(project):
    """Per-project is the point: Aviation has hundreds of tasks, and a per-task
    declaration would miss every one created before it."""
    _run(project, "project", "egress", "grant", "Aviation", "opensky-network.org")
    cp = control_plane_on(dsn_for(project))
    row = cp.store.query_one("SELECT metadata FROM projects WHERE name = ?", ("Aviation",))
    stored = json.loads(row["metadata"])["egress_contract"]
    assert stored["hosts"] == ["opensky-network.org"]


def test_it_reaches_a_task_created_before_the_declaration(project):
    """The projection happens at claim time, so ordering does not matter."""
    cp = control_plane_on(dsn_for(project))
    machine = cp.register_machine("host")
    agent = cp.register_agent(machine.id, "worker-1", capabilities=["python"])
    task = cp.create_task("fly", project="Aviation", required_capabilities=["python"])

    _run(project, "project", "egress", "grant", "Aviation", "tfr.faa.gov")

    cp2 = control_plane_on(dsn_for(project))
    cp2.claim_task(task.id, agent.id)
    assignment = cp2._active_assignment_for_agent(cp2.get_agent(agent.id))
    contract = assignment["task"]["metadata"]["egress_contract"]
    assert contract["hosts"] == ["tfr.faa.gov"]
    assert contract["source"] == "project"
