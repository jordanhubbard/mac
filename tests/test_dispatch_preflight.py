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
    # The per-agent detail carries the reason; the one-line explanation says
    # only that nothing is eligible, because naming another person's private
    # hardware to a stranger would disclose the fleet's shape to them.
    assert theirs["detail"][0]["blocked"] == "agent is private to another owner"


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


# ---------------------------------------------------------------------------
# --as-human must resolve to the id preflight actually compares
# ---------------------------------------------------------------------------


class _FakeHuman:
    def __init__(self, human_id):
        self.id = human_id


class _FakePlane:
    """Just enough of the plane surface that _preflight_filer_id uses.

    Both the direct ControlPlane and the HTTP shim expose these two lookups,
    so resolving through them keeps `mac task preflight` agreeing with
    `mac task create` in either mode.
    """

    def __init__(self, *, username=None, human_id=None):
        self._username = username
        self._human_id = human_id

    def get_human_by_username(self, value):
        if self._username is not None and value == self._username:
            return _FakeHuman(self._human_id)
        raise KeyError(value)

    def get_human(self, value):
        if self._human_id is not None and value == self._human_id:
            return _FakeHuman(self._human_id)
        raise KeyError(value)


def test_as_human_username_resolves_to_the_owner_id():
    """A username must reach the owner comparison as the owner's id.

    Passing the raw CLI string through meant "jordanh" was compared against
    "human_c2ad...", so a private agent was reported "private to another owner"
    to its own owner. Live on 2026-08-17 that under-reported the fleet by three
    static hosts (4 eligible by username vs 7 by id).
    """
    from mac.cli import _preflight_filer_id

    plane = _FakePlane(username="jordanh", human_id="human_c2ad4885")
    assert _preflight_filer_id(plane, "jordanh") == "human_c2ad4885"


def test_as_human_accepts_the_id_form_unchanged():
    from mac.cli import _preflight_filer_id

    plane = _FakePlane(username="jordanh", human_id="human_c2ad4885")
    assert _preflight_filer_id(plane, "human_c2ad4885") == "human_c2ad4885"


def test_both_forms_give_the_same_eligibility_for_a_private_owner():
    """The username and id forms must not disagree about capacity."""
    from mac.cli import _preflight_filer_id

    plane = _FakePlane(username="jordanh", human_id="human_c2ad4885")
    fleet = [_agent("rocky", ("python",), visibility="private", owner="human_c2ad4885")]

    by_name = preflight(
        fleet,
        required_capabilities=["python"],
        created_by_human=_preflight_filer_id(plane, "jordanh"),
    )
    by_id = preflight(
        fleet,
        required_capabilities=["python"],
        created_by_human=_preflight_filer_id(plane, "human_c2ad4885"),
    )
    assert by_name["eligible_agents"] == by_id["eligible_agents"] == ["rocky"]


def test_an_unresolvable_filer_refuses_rather_than_under_reporting():
    """Silently degrading to "no filer" is what hides owned capacity."""
    import pytest

    from mac.cli import _preflight_filer_id

    plane = _FakePlane(username="jordanh", human_id="human_c2ad4885")
    with pytest.raises(SystemExit):
        _preflight_filer_id(plane, "nobody")


def test_absent_as_human_still_means_no_filer():
    from mac.cli import _preflight_filer_id

    plane = _FakePlane(username="jordanh", human_id="human_c2ad4885")
    assert _preflight_filer_id(plane, None) is None
    assert _preflight_filer_id(plane, "  ") is None
