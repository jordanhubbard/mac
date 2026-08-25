"""A sandbox guardrail change is announced on the bus, with its DIRECTION.

The distinction these tests exist to pin is not "did the policy change" — a
checksum already answers that, and a worker that learns only that much has to
either ignore it or stop for every comment edit. It is which way it moved:

* the new policy REVOKES something the running sandbox still holds, so that
  sandbox is now over-permissioned relative to what a human approved; or
* the new policy only GRANTS, so the running sandbox is still compliant and
  merely less capable than a fresh one.

The diff is structural (over parsed YAML), because a text diff answers a
different question: reordering blocks or editing a comment rewrites every line
and revokes nothing, and a signal that fires on that trains its consumer to
ignore it.
"""

from __future__ import annotations

import pytest

from mac.agentbus_broadcast import (
    BROADCAST_EVENT_TYPE_SET,
    BROADCAST_MAX_PAYLOAD_BYTES,
    BROADCAST_MAX_PAYLOAD_KEYS,
    BROADCAST_MAX_VALUE_CHARS,
    BROADCAST_SYSTEM_AGENT_ID,
)
from mac.models import NotFoundError, json_dumps
from mac.openshell_policy_diff import (
    diff_policy_texts,
    policy_change_payload,
)
from mac.services import ControlPlane

WIDE = """version: 1

filesystem_policy:
  include_workdir: true
  read_only:
    - /usr
  read_write:
    - /tmp

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
    binaries:
      - { path: /usr/bin/python3 }
  github:
    name: github
    endpoints:
      - host: github.com
        port: 443
    binaries:
      - { path: /usr/bin/git }
"""

# Same policy, github revoked: an endpoint and a binary the running sandbox
# still holds are gone.
NARROW = """version: 1

filesystem_policy:
  include_workdir: true
  read_only:
    - /usr
  read_write:
    - /tmp

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
    binaries:
      - { path: /usr/bin/python3 }
"""

# Same policy plus one endpoint: nothing the running sandbox holds is revoked.
WIDER = (
    WIDE
    + """  pypi:
    name: pypi
    endpoints:
      - host: pypi.org
        port: 443
"""
)


def _agent(cp: ControlPlane):
    machine = cp.register_machine("rocky", resources={"openshell_required": True})
    return cp.register_agent(
        machine.id, "rocky", capabilities=["python"], resources={"openshell_required": True}
    )


def _policy_events(cp: ControlPlane, agent_id: str):
    return [
        event
        for event in cp.read_agentbus_broadcasts(agent_id, limit=500)
        if str(event["event_type"]).startswith("sandbox.policy")
    ]


# ---------------------------------------------------------------------------
# The diff, in isolation: direction, not change
# ---------------------------------------------------------------------------


def test_a_revocation_restricts():
    diff = diff_policy_texts(WIDE, NARROW)

    assert diff["restricts"] is True
    assert diff["expands"] is False
    assert diff["endpoints_removed"] == 1
    assert diff["binaries_removed"] == 1
    assert diff["endpoints_added"] == 0


def test_a_pure_addition_does_not_restrict():
    diff = diff_policy_texts(WIDE, WIDER)

    assert diff["restricts"] is False
    assert diff["expands"] is True
    assert diff["endpoints_added"] == 1
    assert diff["endpoints_removed"] == 0


def test_a_swap_is_both_a_revocation_and_a_grant():
    """Representable on purpose: "changed" would hide the revoked half."""
    swapped = WIDE.replace("github.com", "gitlab.com")

    diff = diff_policy_texts(WIDE, swapped)

    assert diff["restricts"] is True
    assert diff["expands"] is True


def test_reordering_and_comments_move_nothing():
    """The false alarm a text diff would raise, and the reason this is structural."""
    reordered = """version: 1

# a comment that did not exist before
network_policies:
  github:
    name: github
    endpoints:
      - {host: github.com, port: 443}
    binaries:
      - /usr/bin/git
  mac_hub:
    name: mac-hub
    binaries:
      - { path: /usr/bin/python3 }
    endpoints:
      - host: hub.example.com
        port: 8789

filesystem_policy:
  read_write: [/tmp]
  read_only: [/usr]
  include_workdir: true
"""

    diff = diff_policy_texts(WIDE, reordered)

    assert diff["restricts"] is False
    assert diff["expands"] is False


def test_demoting_a_writable_path_is_a_revocation():
    """rw -> ro is a revocation, not a rename; the mode is part of the identity."""
    demoted = WIDE.replace("  read_write:\n    - /tmp", "  read_write: []").replace(
        "  read_only:\n    - /usr", "  read_only:\n    - /usr\n    - /tmp"
    )

    diff = diff_policy_texts(WIDE, demoted)

    assert diff["restricts"] is True
    assert diff["paths_removed"] == 1


def test_an_unreadable_policy_fails_safe():
    """We cannot show that nothing was revoked, so we must not claim it."""
    diff = diff_policy_texts(WIDE, "network_policies: [oops\n")

    assert diff["parsed"] is False
    assert diff["restricts"] is True


def test_losing_the_policy_entirely_restricts():
    diff = diff_policy_texts(WIDE, None)

    assert diff["restricts"] is True
    assert diff["expands"] is False


# ---------------------------------------------------------------------------
# The payload: small enough to be an announcement, complete enough to decide on
# ---------------------------------------------------------------------------


def test_the_payload_fits_the_caps_and_is_not_truncated():
    """The module refuses silent truncation; a partial answer must never ship.

    Checked through the real publish path, since that is where the bounding
    happens — asserting on the dict we built would prove nothing about what a
    consumer receives.
    """
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.create_openshell_policy("x" * 120, WIDE, created_by="operator")
    cp.assign_openshell_policy(policy.id, target_type="agent", target_id=agent.id)

    events = _policy_events(cp, agent.id)
    assert events
    for event in events:
        payload = event["payload"]
        assert "truncated" not in payload
        assert len(payload) <= BROADCAST_MAX_PAYLOAD_KEYS
        assert len(json_dumps(payload).encode("utf-8")) <= BROADCAST_MAX_PAYLOAD_BYTES
        for value in payload.values():
            assert not isinstance(value, str) or len(value) <= BROADCAST_MAX_VALUE_CHARS
        # Detail is FETCHED, never embedded: the bus is announcements.
        assert "policy_text" not in payload


def test_the_payload_answers_what_a_worker_has_to_decide():
    payload = policy_change_payload(
        change_kind="updated",
        policy_id="ospol-1",
        policy_name="default",
        from_text=WIDE,
        to_text=NARROW,
        from_version=3,
        to_version=4,
        from_checksum="sha256:aaa",
        to_checksum="sha256:bbb",
        target_type="policy",
        target_id="ospol-1",
    )

    assert payload["restricts"] is True
    assert payload["action_hint"] == "abandon_current"
    assert payload["from_version"] == 3 and payload["to_version"] == 4
    assert payload["to_checksum"] == "sha256:bbb"


def test_a_widening_asks_only_for_a_fresh_sandbox_next_time():
    payload = policy_change_payload(
        change_kind="updated",
        policy_id="ospol-1",
        from_text=WIDE,
        to_text=WIDER,
        to_version=2,
    )

    assert payload["restricts"] is False
    assert payload["action_hint"] == "recreate_before_next_task"


def test_an_unknown_change_kind_is_refused():
    with pytest.raises(ValueError):
        policy_change_payload(change_kind="mutated", policy_id="ospol-1")


# ---------------------------------------------------------------------------
# The hub emits, from the paths that actually performed the change
# ---------------------------------------------------------------------------


def test_the_verbs_are_in_the_closed_vocabulary():
    assert "sandbox.policy_changed" in BROADCAST_EVENT_TYPE_SET
    assert "sandbox.policy_published" in BROADCAST_EVENT_TYPE_SET


def test_creating_a_policy_publishes_but_changes_nothing_in_effect():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)

    policy = cp.create_openshell_policy("default", WIDE, created_by="operator")

    events = _policy_events(cp, agent.id)
    assert [event["event_type"] for event in events] == ["sandbox.policy_published"]
    assert events[0]["agent_id"] == BROADCAST_SYSTEM_AGENT_ID
    assert events[0]["payload"]["policy_id"] == policy.id
    assert events[0]["payload"]["change_kind"] == "published"


def test_restricting_an_update_announces_the_revocation_and_the_publication():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.create_openshell_policy("default", WIDE, created_by="operator")
    cp.assign_openshell_policy(policy.id, target_type="agent", target_id=agent.id)

    cp.update_openshell_policy(policy.id, policy_text=NARROW, updated_by="operator")

    kinds = {
        event["event_type"]: event["payload"]
        for event in _policy_events(cp, agent.id)
        if event["payload"].get("change_kind") == "updated"
    }
    assert set(kinds) == {"sandbox.policy_changed", "sandbox.policy_published"}
    changed = kinds["sandbox.policy_changed"]
    assert changed["restricts"] is True
    assert changed["expands"] is False
    assert changed["action_hint"] == "abandon_current"
    assert changed["from_version"] == 1 and changed["to_version"] == 2
    assert changed["to_checksum"] == cp.get_openshell_policy(policy.id).checksum


def test_a_metadata_only_update_says_nothing():
    """A rename moves no capability. A bus that reports it teaches its own noise."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.create_openshell_policy("default", WIDE, created_by="operator")
    before = len(_policy_events(cp, agent.id))

    cp.update_openshell_policy(policy.id, description="now documented")

    assert len(_policy_events(cp, agent.id)) == before


def test_assigning_a_narrower_policy_names_the_target_and_the_revocation():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    wide = cp.create_openshell_policy("wide", WIDE, created_by="operator")
    narrow = cp.create_openshell_policy("narrow", NARROW, created_by="operator")
    cp.assign_openshell_policy(wide.id, target_type="agent", target_id=agent.id)

    cp.assign_openshell_policy(narrow.id, target_type="agent", target_id=agent.id)

    assigned = [
        event["payload"]
        for event in _policy_events(cp, agent.id)
        if event["payload"].get("change_kind") == "assigned"
        and event["payload"].get("policy_id") == narrow.id
    ]
    assert len(assigned) == 1
    assert assigned[0]["target_type"] == "agent"
    assert assigned[0]["target_id"] == agent.id
    assert assigned[0]["restricts"] is True
    assert assigned[0]["from_checksum"] == wide.checksum


def test_a_first_assignment_grants_and_does_not_revoke():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.create_openshell_policy("wide", WIDE, created_by="operator")

    cp.assign_openshell_policy(policy.id, target_type="agent", target_id=agent.id)

    assigned = [
        event["payload"]
        for event in _policy_events(cp, agent.id)
        if event["payload"].get("change_kind") == "assigned"
    ]
    assert assigned[0]["restricts"] is False
    assert assigned[0]["expands"] is True
    assert assigned[0]["action_hint"] == "recreate_before_next_task"


def test_deleting_a_policy_tells_every_target_it_lost_its_guardrail():
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    policy = cp.create_openshell_policy("wide", WIDE, created_by="operator")
    cp.assign_openshell_policy(policy.id, target_type="agent", target_id=agent.id)

    cp.delete_openshell_policy(policy.id)

    payloads = [event["payload"] for event in _policy_events(cp, agent.id)]
    deleted = [p for p in payloads if p.get("change_kind") == "deleted"]
    unassigned = [p for p in payloads if p.get("change_kind") == "unassigned"]
    assert deleted and deleted[0]["restricts"] is True
    assert unassigned and unassigned[0]["target_id"] == agent.id
    assert unassigned[0]["action_hint"] == "abandon_current"


def test_announcing_never_breaks_the_write(monkeypatch):
    """The policy write is already durable; a broken bus must not undo it."""
    cp = ControlPlane.in_memory()
    agent = _agent(cp)

    def _explode(*_args, **_kwargs):
        raise RuntimeError("bus down")

    monkeypatch.setattr(cp.agentbus_broadcast, "publish_system", _explode)
    policy = cp.create_openshell_policy("default", WIDE, created_by="operator")

    assert cp.get_openshell_policy(policy.id).checksum == policy.checksum
    assert _policy_events(cp, agent.id) == []


def test_the_hub_speaks_as_itself_and_no_token_can():
    """publish_system has no HTTP route behind it; the agent route still checks.

    If the hub announced as the affected agent, that agent would see its own
    "echo" and skip the one event it exists to act on.
    """
    cp = ControlPlane.in_memory()
    agent = _agent(cp)
    cp.create_openshell_policy("default", WIDE, created_by="operator")

    event = _policy_events(cp, agent.id)[0]
    assert event["agent_id"] == BROADCAST_SYSTEM_AGENT_ID
    assert event["self_emitted"] is False
    with pytest.raises(NotFoundError):
        cp.publish_agentbus_broadcast(
            BROADCAST_SYSTEM_AGENT_ID, "sandbox.policy_changed", payload={}
        )
