"""A registration refused for size must be diagnosable from the hub.

The incident these tests exist for: a worker's ``machine.resources`` crossed the
64 KB limit, the hub refused ``POST /machines``, the worker exited 1, systemd
restarted it, and the loop ran all day. ``systemctl is-active`` said ``active``;
``mac agent list`` said ``offline``, which is what a powered-off host says. The
only place the reason existed was the failing host's own journal.

So the properties asserted here are operator-facing, not internal:

* a 64 KB + 1 registration is refused **and leaves a hub-side record naming the
  size and the largest contributor** (``test_a_refused_registration_is_diagnosable_from_the_hub_alone``);
* ``mac agent list`` renders refused differently from offline and from absent;
* pressure is reported *before* the limit, and does not write a row per poll;
* the block that grows without bound — the command inventory — is bounded in
  bytes, and a worker refused anyway sheds and joins rather than exiting.
"""

from __future__ import annotations

import argparse
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from mac import cli, payload_budget, worker
from mac.api import MAX_REGISTRATION_PAYLOAD_BYTES, create_app
from mac.api_client import MacApiError
from mac.models import ValidationError
from mac.observability_console import build_console_snapshot
from mac.registration_budget import (
    PRESSURE_EVENT,
    REFUSAL_EVENT,
    annotate_agent_rows,
    enforce,
    list_refusals,
)
from mac.services import ControlPlane


@pytest.fixture()
def cp() -> ControlPlane:
    return ControlPlane.in_memory()


def _resources_of_size(target_bytes: int, key: str = "commands") -> dict:
    """A resources dict whose compact JSON encoding is exactly ``target_bytes``."""
    payload = {key: ""}
    overhead = len(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return {key: "x" * max(0, target_bytes - overhead)}


# ---------------------------------------------------------------------------
# The measurement primitive
# ---------------------------------------------------------------------------


def test_measure_reports_the_band_and_names_what_is_consuming_the_payload():
    resources = {
        "commands": ["cmd%04d" % index for index in range(400)],
        "hardware": {"cpu": "m3", "memory_gb": 64},
        "dispatch_policy": {"allowed_projects": ["mac"]},
    }
    budget = payload_budget.measure(
        resources, field_name="machine.resources", limit_bytes=8 * 1024
    )
    assert budget.size_bytes == payload_budget.encoded_size(resources)
    assert budget.band in payload_budget.BANDS
    # The point of contributors: "resources is big" is not actionable, "commands
    # is 90% of it" is.
    assert budget.contributors[0]["key"] == "commands"
    assert budget.contributors[0]["share"] > 0.8
    assert "commands" in budget.describe()


@pytest.mark.parametrize(
    "utilization,expected",
    [
        (0.0, payload_budget.BAND_OK),
        (0.74, payload_budget.BAND_OK),
        (0.75, payload_budget.BAND_WARN),
        (0.89, payload_budget.BAND_WARN),
        (0.90, payload_budget.BAND_CRITICAL),
        (1.0, payload_budget.BAND_CRITICAL),
        (1.0001, payload_budget.BAND_OVER),
    ],
)
def test_band_boundaries_warn_well_before_the_fuse(utilization, expected):
    assert payload_budget.band_for(utilization) == expected


def test_a_hostile_key_name_is_not_echoed_back_at_full_length():
    """The measured payload is untrusted, and its key names come back out."""
    budget = payload_budget.measure({"k" * 5000: "v"}, limit_bytes=1024)
    assert len(budget.contributors[0]["key"]) == payload_budget.MAX_CONTRIBUTOR_KEY_CHARS
    assert len(budget.describe()) < 500


def test_shed_drops_the_heaviest_unprotected_block_and_says_what_it_dropped():
    payload = {
        "commands": "x" * 4000,
        "media_routes": "y" * 1000,
        "dispatch_policy": {"allowed_projects": ["mac"]},
    }
    reduced, report = payload_budget.shed_to_budget(
        payload, limit_bytes=2000, target_utilization=0.75, protected=("dispatch_policy",)
    )
    assert "commands" not in reduced
    assert reduced["dispatch_policy"] == payload["dispatch_policy"]
    assert [item["key"] for item in report["shed"]] == ["commands"]
    assert report["fits"] is True
    assert report["reason"]


def test_shed_refuses_to_mutilate_protected_state_to_force_a_fit():
    """A payload that only fits without its credential is not a registration."""
    payload = {"worker_credential": "x" * 5000}
    reduced, report = payload_budget.shed_to_budget(
        payload, limit_bytes=1000, protected=("worker_credential",)
    )
    assert reduced == payload
    assert report["fits"] is False
    assert report["shed"] == []


def test_string_list_is_bounded_in_bytes_not_only_in_count():
    names = ["command-name-%04d" % index for index in range(5000)]
    kept, omitted = payload_budget.bounded_string_list(names, max_bytes=1024)
    assert omitted > 0
    assert len(kept) + omitted == len(names)
    assert payload_budget.encoded_size(kept) <= 1024
    # Order is preserved, so a caller can put the names it cares about first.
    assert kept == names[: len(kept)]


# ---------------------------------------------------------------------------
# The acceptance criterion: diagnose a 64KB+1 refusal from the hub alone
# ---------------------------------------------------------------------------


def test_a_refused_registration_is_diagnosable_from_the_hub_alone(cp: ControlPlane):
    client = TestClient(create_app(control_plane=cp))
    oversized = _resources_of_size(MAX_REGISTRATION_PAYLOAD_BYTES + 1)

    response = client.post(
        "/machines",
        json={"hostname": "worker-a", "machine_id": "machine-a", "resources": oversized},
    )
    assert response.status_code == 400
    # No row was written -- that is the whole reason the hub used to forget.
    assert client.get("/machines").json() == []

    # Everything an operator needs, from the hub, without touching the host.
    refusals = client.get("/agents/registration-refusals").json()
    assert len(refusals) == 1
    refusal = refusals[0]
    assert refusal["hostname"] == "worker-a"
    assert refusal["field"] == "machine.resources"
    assert refusal["size_bytes"] == MAX_REGISTRATION_PAYLOAD_BYTES + 1
    assert refusal["limit_bytes"] == MAX_REGISTRATION_PAYLOAD_BYTES
    assert refusal["band"] == payload_budget.BAND_OVER
    assert refusal["top_contributors"][0]["key"] == "commands"
    assert "commands" in refusal["message"]


def test_a_crash_loop_is_counted_not_repeated(cp: ControlPlane):
    """47 restarts must read as one host refused 47 times, not 47 hosts."""
    client = TestClient(create_app(control_plane=cp))
    oversized = _resources_of_size(MAX_REGISTRATION_PAYLOAD_BYTES + 1)
    for _ in range(5):
        client.post("/machines", json={"hostname": "worker-a", "resources": oversized})

    refusals = client.get("/agents/registration-refusals").json()
    assert len(refusals) == 1
    assert refusals[0]["refusal_count"] == 5
    assert refusals[0]["first_refused_at"] <= refusals[0]["last_refused_at"]


def test_the_refusal_message_carries_the_diagnosis_not_just_the_limit(cp: ControlPlane):
    with pytest.raises(ValidationError) as excinfo:
        enforce(
            cp,
            _resources_of_size(2048),
            "machine.resources",
            subject_type="machine",
            hostname="worker-a",
            limit_bytes=1024,
        )
    message = str(excinfo.value)
    assert "machine.resources exceeds 1024-byte limit" in message
    assert "commands" in message


# ---------------------------------------------------------------------------
# Refused is not offline, and it is not absent
# ---------------------------------------------------------------------------


def _agent_list_rows(cp: ControlPlane) -> list:
    """Run `mac agent list` against ``cp`` and capture what it printed."""
    printed: list = []
    args = argparse.Namespace(health=False, selector=None)
    original_plane, original_print = cli._plane, cli._print
    cli._plane = lambda _args: cp
    cli._print = printed.append
    try:
        cli.cmd_agent_list(args)
    finally:
        cli._plane, cli._print = original_plane, original_print
    return printed[0]


def test_agent_list_separates_refused_from_offline_and_from_absent(cp: ControlPlane):
    machine = cp.register_machine(hostname="worker-b")
    healthy = cp.register_agent(machine.id, "worker-b", capabilities=["python"])
    cp.store.execute(
        "UPDATE agents SET status = 'offline' WHERE id = ?", (healthy.id,)
    )
    client = TestClient(create_app(control_plane=cp))
    client.post(
        "/machines",
        json={
            "hostname": "worker-a",
            "resources": _resources_of_size(MAX_REGISTRATION_PAYLOAD_BYTES + 1),
        },
    )

    rows = {row["name"]: row for row in _agent_list_rows(cp)}

    # The switched-off host: a real row, accepted, merely quiet.
    assert rows["worker-b"]["status"] == "offline"
    assert rows["worker-b"]["registration_state"] == "accepted"
    assert rows["worker-b"]["registered"] is True

    # The refused host: no agent row exists, so before this it was invisible.
    refused = rows["worker-a"]
    assert refused["registration_state"] == "refused"
    assert refused["registered"] is False
    assert refused["status"] == "refused"
    assert refused["registration_refusal"]["size_bytes"] > MAX_REGISTRATION_PAYLOAD_BYTES

    # And a host that simply does not exist is still absent -- we did not invent it.
    assert "worker-c" not in rows


def test_agent_list_annotates_an_existing_agent_that_is_now_being_refused(
    cp: ControlPlane,
):
    """The other half of the incident: a worker that joined once and now cannot."""
    machine = cp.register_machine(hostname="worker-a")
    agent = cp.register_agent(machine.id, "worker-a", capabilities=[])
    client = TestClient(create_app(control_plane=cp))
    client.post(
        "/agents",
        json={
            "machine_id": machine.id,
            "name": "worker-a",
            "agent_id": agent.id,
            "resources": _resources_of_size(MAX_REGISTRATION_PAYLOAD_BYTES + 1),
        },
    )

    rows = {row["name"]: row for row in _agent_list_rows(cp)}
    assert rows["worker-a"]["registration_state"] == "refused"
    # It has a row, so it is registered -- but its latest attempt was turned away.
    assert rows["worker-a"]["registered"] is True
    assert rows["worker-a"]["registration_refusal"]["field"] == "agent.resources"


def test_agent_list_still_works_against_a_plane_that_cannot_report_refusals():
    """An older hub degrades to the previous output rather than failing."""
    plane = SimpleNamespace(list_agents=lambda: [{"id": "a1", "name": "a1", "resources": {}}])
    printed: list = []
    args = argparse.Namespace(health=False, selector=None)
    original_plane, original_print = cli._plane, cli._print
    cli._plane = lambda _args: plane
    cli._print = printed.append
    try:
        cli.cmd_agent_list(args)
    finally:
        cli._plane, cli._print = original_plane, original_print
    assert printed[0][0]["registration_state"] == "accepted"


def test_the_console_shows_refused_as_its_own_state(cp: ControlPlane):
    client = TestClient(create_app(control_plane=cp))
    client.post(
        "/machines",
        json={
            "hostname": "worker-a",
            "resources": _resources_of_size(MAX_REGISTRATION_PAYLOAD_BYTES + 1),
        },
    )
    agents = build_console_snapshot(cp)["agents"]
    assert agents["refused"] == 1
    assert agents["refusals"][0]["hostname"] == "worker-a"
    row = next(r for r in agents["rows"] if r["registration_state"] == "refused")
    assert row["registered"] is False
    # `total` counts agent ROWS; a refused host has none and must not inflate it.
    assert agents["total"] == 0


def test_annotation_does_not_claim_a_refusal_belongs_to_an_unrelated_agent():
    rows = [{"id": "agent_rocky", "name": "rocky", "machine_id": "m1", "resources": {}}]
    refusals = [{"subject_id": "agent_natasha", "hostname": "natasha", "agent_id": "agent_natasha"}]
    annotated = annotate_agent_rows(rows, refusals)
    by_name = {row["name"]: row for row in annotated}
    assert by_name["rocky"]["registration_state"] == "accepted"
    assert by_name["natasha"]["registration_state"] == "refused"


# ---------------------------------------------------------------------------
# The gauge: warn before the fuse, without writing a row per poll
# ---------------------------------------------------------------------------


def test_pressure_is_reported_before_the_limit_is_crossed(cp: ControlPlane):
    limit = 4096
    enforce(
        cp,
        _resources_of_size(int(limit * 0.80)),
        "machine.resources",
        subject_type="machine",
        hostname="worker-a",
        limit_bytes=limit,
    )
    events = cp.list_observability(kind="metric", name=PRESSURE_EVENT, limit=10)
    assert len(events) == 1
    assert events[0].detail["band"] == payload_budget.BAND_WARN
    assert events[0].detail["previous_band"] == payload_budget.BAND_OK
    assert events[0].detail["top_contributors"][0]["key"] == "commands"
    assert events[0].value == pytest.approx(0.80, abs=0.01)


def test_pressure_writes_on_band_change_only(cp: ControlPlane):
    """A row per registration is how observability_events reached 3.1GB before."""
    limit = 4096
    for _ in range(4):
        enforce(
            cp,
            _resources_of_size(int(limit * 0.80)),
            "machine.resources",
            subject_type="machine",
            hostname="worker-a",
            limit_bytes=limit,
        )
    assert len(cp.list_observability(kind="metric", name=PRESSURE_EVENT, limit=10)) == 1

    # Crossing into critical is a change, so it is recorded.
    enforce(
        cp,
        _resources_of_size(int(limit * 0.95)),
        "machine.resources",
        subject_type="machine",
        hostname="worker-a",
        limit_bytes=limit,
    )
    events = cp.list_observability(kind="metric", name=PRESSURE_EVENT, limit=10)
    assert [event.detail["band"] for event in events] == [
        payload_budget.BAND_CRITICAL,
        payload_budget.BAND_WARN,
    ]

    # And so is recovering, so a resolved alarm does not look permanent.
    enforce(
        cp,
        _resources_of_size(int(limit * 0.10)),
        "machine.resources",
        subject_type="machine",
        hostname="worker-a",
        limit_bytes=limit,
    )
    assert cp.list_observability(kind="metric", name=PRESSURE_EVENT, limit=1)[0].detail[
        "band"
    ] == payload_budget.BAND_OK


def test_a_healthy_registration_records_nothing(cp: ControlPlane):
    enforce(
        cp,
        {"hardware": {"cpu": "m3"}},
        "machine.resources",
        subject_type="machine",
        hostname="worker-a",
    )
    assert cp.list_observability(name=PRESSURE_EVENT, limit=5) == []
    assert cp.list_observability(name=REFUSAL_EVENT, limit=5) == []


def test_reporting_failures_never_break_a_registration(cp: ControlPlane):
    """The limit check is load-bearing; the record of it is best-effort."""

    class Exploding:
        def record_observation(self, **_kwargs):
            raise RuntimeError("observability is down")

        def list_observability(self, **_kwargs):
            raise RuntimeError("observability is down")

    budget = enforce(
        Exploding(), {"a": 1}, "machine.resources", subject_type="machine", hostname="h"
    )
    assert budget.band == payload_budget.BAND_OK
    with pytest.raises(ValidationError):
        enforce(
            Exploding(),
            _resources_of_size(2048),
            "machine.resources",
            subject_type="machine",
            hostname="h",
            limit_bytes=1024,
        )
    assert list_refusals(Exploding()) == []


def test_the_heartbeat_reports_growth_but_never_refuses_a_working_agent(
    cp: ControlPlane,
):
    """Growth happens between restarts, so the gauge has to read on heartbeat.

    Enforcing here would take an agent the hub already admitted offline over a
    payload it already stored — the same incident with a different trigger.
    """
    machine = cp.register_machine(hostname="worker-a")
    agent = cp.register_agent(machine.id, "worker-a", capabilities=[])
    client = TestClient(create_app(control_plane=cp))

    heavy = _resources_of_size(int(MAX_REGISTRATION_PAYLOAD_BYTES * 0.92))
    response = client.post(
        "/agents/%s/heartbeat" % agent.id, json={"resources": heavy}
    )
    assert response.status_code == 200

    events = cp.list_observability(kind="metric", name=PRESSURE_EVENT, limit=5)
    assert len(events) == 1
    assert events[0].detail["band"] == payload_budget.BAND_CRITICAL
    assert events[0].subject_id == agent.id
    assert events[0].detail["top_contributors"][0]["key"] == "commands"


def test_a_healthy_heartbeat_costs_no_database_read(cp: ControlPlane):
    """Below the warn band the gauge must not query history on a 1Hz path."""

    class CountingReads:
        def __init__(self) -> None:
            self.reads = 0

        def list_observability(self, **_kwargs):
            self.reads += 1
            return []

        def record_observation(self, **_kwargs):
            raise AssertionError("a healthy payload must record nothing")

    plane = CountingReads()
    from mac.registration_budget import observe_pressure

    budget = observe_pressure(
        plane,
        {"hardware": {"cpu": "m3"}},
        "agent.resources",
        subject_type="agent",
        subject_id="agent_a",
    )
    assert budget.band == payload_budget.BAND_OK
    assert plane.reads == 0


def test_an_accepted_registration_returns_the_gauge_to_the_worker(cp: ControlPlane):
    client = TestClient(create_app(control_plane=cp))
    body = client.post(
        "/machines", json={"hostname": "worker-a", "resources": {"hardware": {"cpu": "m3"}}}
    ).json()
    assert body["resources_budget"]["limit_bytes"] == MAX_REGISTRATION_PAYLOAD_BYTES
    assert body["resources_budget"]["band"] == payload_budget.BAND_OK
    assert body["resources_budget"]["remaining_bytes"] > 0


# ---------------------------------------------------------------------------
# Worker side: bound the grower, shed instead of exiting
# ---------------------------------------------------------------------------


def test_command_inventory_is_bounded_in_bytes(monkeypatch, tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for index in range(400):
        entry = binaries / ("a-rather-long-command-name-%04d" % index)
        entry.write_text("#!/bin/sh\n", encoding="utf-8")
        entry.chmod(0o755)
    monkeypatch.setattr(worker.os, "get_exec_path", lambda: [str(binaries)])
    monkeypatch.setenv("MAC_WORKER_COMMAND_INVENTORY_MAX_BYTES", "2048")

    inventory = worker._detect_command_inventory()
    assert payload_budget.encoded_size(inventory["available"]) <= 2048
    assert inventory["truncated"] is True
    assert inventory["bounded_by"] == "bytes"
    assert inventory["omitted"] > 0
    # Bounded far below the hub's limit, so this block alone can never refuse
    # the worker entry to the fleet.
    assert payload_budget.encoded_size(inventory) < MAX_REGISTRATION_PAYLOAD_BYTES


def test_command_inventory_keeps_explicitly_probed_names_first(monkeypatch, tmp_path):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for name in ["git"] + ["filler-%04d" % index for index in range(200)]:
        entry = binaries / name
        entry.write_text("#!/bin/sh\n", encoding="utf-8")
        entry.chmod(0o755)
    monkeypatch.setattr(worker.os, "get_exec_path", lambda: [str(binaries)])
    monkeypatch.setattr(worker.shutil, "which", lambda name: str(binaries / name) if name == "git" else None)
    monkeypatch.setenv("MAC_WORKER_COMMAND_INVENTORY_MAX_BYTES", "512")

    inventory = worker._detect_command_inventory()
    # Truncation costs the incidental tail, never the toolchain a contract asks about.
    assert "git" in inventory["available"]
    assert inventory["omitted"] > 0


def test_worker_measures_its_payload_and_sheds_before_the_hub_has_to_refuse(caplog):
    resources = {
        "commands": {"available": ["cmd%05d" % index for index in range(12000)]},
        "dispatch_policy": {"allowed_projects": ["mac"]},
        "worker_credential": {"proof": "signed"},
    }
    assert payload_budget.encoded_size(resources) > MAX_REGISTRATION_PAYLOAD_BYTES

    bounded, budget = worker._bounded_registration_resources(
        resources, "machine.resources"
    )
    assert budget.band == payload_budget.BAND_OK
    assert "commands" not in bounded
    # What was dropped, and why, travels with the registration that succeeds.
    assert bounded["payload_budget"]["shed"]["shed"][0]["key"] == "commands"
    assert bounded["dispatch_policy"] == resources["dispatch_policy"]
    assert bounded["worker_credential"] == resources["worker_credential"]


def test_a_payload_with_headroom_is_sent_untouched_but_measured():
    resources = {"hardware": {"cpu": "m3"}}
    bounded, budget = worker._bounded_registration_resources(
        resources, "machine.resources"
    )
    assert bounded["hardware"] == resources["hardware"]
    assert budget.band == payload_budget.BAND_OK
    assert bounded["payload_budget"]["size_bytes"] == budget.size_bytes
    assert "shed" not in bounded["payload_budget"]


def test_a_refused_worker_sheds_and_joins_instead_of_exiting():
    """The crash loop, prevented: degraded and present beats correct and absent."""
    attempts = []

    class Hub:
        def post(self, path, body):
            attempts.append(body)
            if len(attempts) == 1:
                raise MacApiError("machine.resources exceeds 65536-byte limit -- ...")
            return {"id": "machine-a"}

    body = {
        "hostname": "worker-a",
        "resources": {
            "commands": {"available": ["cmd%05d" % index for index in range(12000)]},
            "dispatch_policy": {"allowed_projects": ["mac"]},
        },
    }
    result = worker._post_registration(Hub(), "/machines", body)

    assert result == {"id": "machine-a"}
    assert len(attempts) == 2
    assert "commands" not in attempts[1]["resources"]
    assert attempts[1]["resources"]["dispatch_policy"] == body["resources"]["dispatch_policy"]
    assert attempts[1]["resources"]["payload_budget"]["shed_after_refusal"] is True


def test_a_refusal_that_is_not_about_size_is_not_retried():
    calls = []

    class Hub:
        def post(self, path, body):
            calls.append(body)
            raise MacApiError("machine_id is required")

    with pytest.raises(MacApiError, match="machine_id is required"):
        worker._post_registration(Hub(), "/machines", {"resources": {"a": "b" * 100}})
    assert len(calls) == 1


def test_a_worker_with_nothing_sheddable_left_reports_rather_than_looping():
    """Retried once, not forever: an unsheddable refusal is a different bug."""
    calls = []

    class Hub:
        def post(self, path, body):
            calls.append(body)
            raise MacApiError("machine.resources exceeds 65536-byte limit -- ...")

    with pytest.raises(MacApiError):
        worker._post_registration(
            Hub(),
            "/machines",
            {"resources": {"worker_credential": "x" * (MAX_REGISTRATION_PAYLOAD_BYTES + 10)}},
        )
    assert len(calls) == 1
