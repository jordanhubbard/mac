"""Would this task ever be claimed? Answered before it is filed.

A task the fleet cannot satisfy is accepted, queued, and never claimed. It does
not fail; it waits. On 2026-08-08 one waited while eight idle agents watched.

For a caller inside mac that is slow. For literate-ai, which submits with a
deadline and blocks, it is the difference between an error naming the
requirement and a timeout that says nothing.
"""

from __future__ import annotations

from mac.dispatch_preflight import explain, preflight


def _agent(name, caps=(), os_name="linux", arch="x86_64", visibility="shared", owner=None):
    return {
        "id": "agent_" + name,
        "name": name,
        "capabilities": list(caps),
        "visibility": visibility,
        "owner_human_id": owner,
        "resources": {"hardware": {"os": os_name, "cpu_arch": arch}},
    }


FLEET = [
    _agent("rocky", ("python", "testing"), "darwin", "arm64"),
    _agent("worker1", ("python", "testing", "c"), "linux", "x86_64"),
    _agent("natasha", ("python",), "linux", "aarch64"),
]


def test_a_satisfiable_task_is_dispatchable():
    result = preflight(FLEET, required_capabilities=["python"])

    assert result["dispatchable"] is True
    assert set(result["eligible_agents"]) == {"rocky", "worker1", "natasha"}


def test_hardware_narrows_to_the_matching_hosts():
    """os and cpu_arch are PROBED FACTS the fleet already publishes, and the
    allocator already matches them. This is the route a host constraint takes."""
    result = preflight(
        FLEET, required_hardware={"os": ["linux"], "cpu_arch": ["x86_64"]}
    )

    assert result["eligible_agents"] == ["worker1"]


def test_an_os_asked_for_as_a_capability_is_named_as_a_mapping_error():
    """THE failure this exists to prevent. Capabilities are set membership over
    a DECLARED vocabulary; no agent will ever advertise 'linux'. Reporting it as
    a missing skill is true and useless -- the caller needs the form that works.
    """
    result = preflight(FLEET, required_capabilities=["linux"])

    assert result["dispatchable"] is False
    assert result["mapping_errors"][0]["capability"] == "linux"
    assert 'required_hardware={"os": ["linux"]}' in result["mapping_errors"][0]["use_instead"]
    assert 'required_hardware' in explain(result)


def test_an_unknown_capability_names_itself():
    result = preflight(FLEET, required_capabilities=["fortran"])

    assert result["dispatchable"] is False
    assert result["missing_capabilities"] == ["fortran"]
    assert "fortran" in explain(result)


def test_an_impossible_hardware_constraint_explains_the_miss():
    result = preflight(FLEET, required_hardware={"cpu_arch": ["riscv64"]})

    assert result["dispatchable"] is False
    assert any("riscv64" in reason for reason in result["hardware_reasons"])


def test_a_private_agent_is_not_capacity_for_someone_else():
    fleet = [_agent("rocky", ("python",), visibility="private", owner="human_a")]

    mine = preflight(fleet, required_capabilities=["python"], created_by_human="human_a")
    theirs = preflight(fleet, required_capabilities=["python"], created_by_human="human_b")

    assert mine["dispatchable"] is True
    assert theirs["dispatchable"] is False
    assert "private" in explain(theirs)


def test_absent_constraints_match_everything():
    """machine_hardware_satisfies is forward-compatible by design: absent
    constraints match and unknown keys are ignored, so a richer caller does not
    have to know mac's whole vocabulary to route."""
    result = preflight(FLEET, required_hardware={"something_new": ["value"]})

    assert result["dispatchable"] is True


def test_the_explanation_is_usable_verbatim():
    """A blocking caller should be able to raise this string as-is."""
    result = preflight(FLEET, required_capabilities=["linux", "fortran"])

    message = explain(result)
    assert message.startswith("not dispatchable:")
    assert "fortran" in message and "linux" in message


def test_an_empty_fleet_is_not_dispatchable():
    """The degenerate case a preflight must not answer optimistically."""
    result = preflight([], required_capabilities=["python"])

    assert result["dispatchable"] is False
    assert result["agents_considered"] == 0
