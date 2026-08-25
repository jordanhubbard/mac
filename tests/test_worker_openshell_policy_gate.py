"""The worker's outer loop acts on a sandbox guardrail change.

An agent cannot be interrupted mid-task: the coding agent runs as
``subprocess.run(argv, input=prompt)`` with stdin closed, so nothing reaches it
once it has started. The decision point is therefore the OUTER loop — after one
task finishes, before the next is claimed, while the next sandbox does not yet
exist. Declining to claim is the whole enforcement mechanism, and it costs no
work in flight.

What these tests pin:

* a REVOCATION stops the worker claiming under the sandbox it would build from
  the superseded policy;
* the wait ENDS on the publication of the version the change named — not on a
  timeout, which is what "wait for the new policy" degrades into without a
  terminating event;
* the wait is BOUNDED, and giving up records why, because a worker parked
  forever by a hub that never published is an outage;
* a change that grants nothing and revokes nothing does not stop anyone.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from mac import worker
from mac.openshell_service import policy_checksum

WIDE = """version: 1
network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
  github:
    name: github
    endpoints:
      - host: github.com
        port: 443
"""

NARROW = """version: 1
network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
"""


class _Client:
    """Serves the assigned policy and a scripted broadcast feed."""

    def __init__(self, *, policy=None, events=None):
        self.posts = []
        self.policy = policy
        self.events = list(events or [])
        self.broadcast_reads = 0

    def post(self, path, payload):
        self.posts.append((path, payload))
        return {}

    def get(self, path):
        parsed = urlparse(path)
        if parsed.path.endswith("/agentbus/broadcast"):
            self.broadcast_reads += 1
            after = int(parse_qs(parsed.query).get("after_sequence", ["0"])[0])
            return [e for e in self.events if int(e["sequence"]) > after]
        if parsed.path.endswith("/openshell/policy"):
            if self.policy is None:
                raise RuntimeError("no assignment")
            return self.policy
        return {}


def _assigned(text, *, version, policy_id="ospol_1"):
    return {
        "schema": "mac.openshell.assigned_policy.v1",
        "agent_id": "agent-1",
        "policy_id": policy_id,
        "policy_name": "fleet",
        "version": version,
        "checksum": policy_checksum(text),
        "policy_text": text,
    }


def _changed_event(sequence, *, to_text, restricts=True, to_version=2, policy_id="ospol_1"):
    return {
        "sequence": sequence,
        "event_type": "sandbox.policy_changed",
        "agent_id": "hub",
        "payload": {
            "change_kind": "updated",
            "policy_id": policy_id,
            "target_type": "policy",
            "target_id": policy_id,
            "restricts": restricts,
            "expands": not restricts,
            "to_version": to_version,
            "to_checksum": policy_checksum(to_text),
            "action_hint": "abandon_current" if restricts else "recreate_before_next_task",
        },
        "self_emitted": False,
    }


def _published_event(sequence, *, to_text, to_version=2, policy_id="ospol_1"):
    event = _changed_event(sequence, to_text=to_text, to_version=to_version, policy_id=policy_id)
    event["event_type"] = "sandbox.policy_published"
    return event


@pytest.fixture
def mac_home(tmp_path, monkeypatch):
    home = tmp_path / "machome"
    home.mkdir()
    monkeypatch.setattr(worker.mac_paths, "mac_home", lambda: home)
    monkeypatch.delenv("MAC_OPENSHELL_POLICY", raising=False)
    monkeypatch.delenv("MAC_OPENSHELL_POLICY_BUS_GATE", raising=False)
    return home


def _worker(tmp_path, client):
    return worker.MacWorker(
        client,
        "agent-1",
        tmp_path,
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


def _converged(instance, mac_home, text, client):
    """Put the worker in the steady state: the wide policy installed and known."""
    client.policy = _assigned(text, version=1)
    instance._maybe_sync_openshell_policy()
    assert (mac_home / "openshell-policy.yaml").read_text(encoding="utf-8") == text


# ---------------------------------------------------------------------------
# A revocation stops the next claim
# ---------------------------------------------------------------------------


def test_a_revocation_holds_the_worker_before_it_claims(mac_home, tmp_path):
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)
    # The hub announced the revocation; the narrower policy is not servable yet.
    client.events = [_changed_event(11, to_text=NARROW)]

    held = instance._openshell_policy_gate()

    assert held is not None
    assert held.status == "held"
    assert "restricts=True" in held.error
    # ...and the sandbox on disk is still the OLD one: the worker did not
    # quietly proceed on the assumption that a resync had happened.
    assert (mac_home / "openshell-policy.yaml").read_text(encoding="utf-8") == WIDE


def test_the_hold_reaches_the_outer_loop_and_no_task_is_claimed(mac_home, tmp_path, monkeypatch):
    """The gate is only worth anything if it sits ahead of the claim."""
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)
    client.events = [_changed_event(11, to_text=NARROW)]

    claims = []
    monkeypatch.setattr(worker, "_deployment_barrier_state", lambda: (0, False))
    monkeypatch.setattr(worker, "_synchronize_directive_policy", lambda *_a, **_k: None)
    for name, result in (
        ("_heartbeat", None),
        ("_current_dispatch_hold", None),
        ("_process_agentbus_control", {}),
        ("_poll_debug_terminal_sessions", None),
        ("_local_repo_update_dispatch_blocker", None),
        ("_maybe_start_workspace_gc", None),
        ("_maybe_sync_service_claims", None),
        ("_maintain_openclaw_gateway_leases", None),
        ("_process_human_delivery_outbox", None),
        ("_process_review_nudges", None),
        ("apply_pending_repo_update_if_idle", None),
        ("_observe_policy_once", None),
        ("_maybe_hub_load_shed", None),
    ):
        monkeypatch.setattr(instance, name, (lambda _r=result: lambda *_a, **_k: _r)())
    monkeypatch.setattr(instance, "_claim_next_for_agent", lambda *_a, **_k: claims.append(1))

    outcome = instance.run_once()

    assert outcome.status == "held"
    assert claims == []


def test_the_wait_ends_on_the_publication_of_the_named_version(mac_home, tmp_path):
    """The terminating event. Without it this is a timeout poll wearing a hat."""
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)
    client.events = [_changed_event(11, to_text=NARROW)]
    assert instance._openshell_policy_gate() is not None

    # The hub publishes v2 and starts serving it; the worker re-converges and
    # resumes claiming in the same pass.
    client.events.append(_published_event(12, to_text=NARROW))
    client.policy = _assigned(NARROW, version=2)

    assert instance._openshell_policy_gate() is None
    assert (mac_home / "openshell-policy.yaml").read_text(encoding="utf-8") == NARROW


def test_a_publication_of_an_older_version_does_not_end_the_wait(mac_home, tmp_path):
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)
    client.events = [_changed_event(11, to_text=NARROW, to_version=5)]
    assert instance._openshell_policy_gate() is not None

    client.events.append(_published_event(12, to_text=NARROW, to_version=4))

    assert instance._openshell_policy_gate() is not None


def test_the_wait_is_bounded_and_says_why_it_gave_up(mac_home, tmp_path, monkeypatch):
    """A hub that never publishes must not park a worker forever.

    Resuming is a deliberate availability choice over an indefinite stall; the
    recorded reason is what keeps it auditable instead of silent.
    """
    monkeypatch.setenv("MAC_OPENSHELL_POLICY_WAIT_SECONDS", "0")
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)
    client.events = [_changed_event(11, to_text=NARROW)]

    assert instance._openshell_policy_gate() is None
    reason = next(
        payload
        for path, payload in client.posts
        if payload.get("name") == "worker.openshell_policy.gate_expired"
    )
    assert "never became installable" in reason["detail"]["reason"]
    assert reason["detail"]["restricts"] is True


def test_an_unrelated_policy_does_not_stop_this_worker(mac_home, tmp_path):
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)
    client.events = [
        _changed_event(11, to_text=NARROW, policy_id="ospol_other"),
    ]

    assert instance._openshell_policy_gate() is None


def test_a_change_aimed_at_this_agent_by_name_is_heard(mac_home, tmp_path):
    """Assignments name the agent; policy edits name the policy. Both are ours."""
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)
    event = _changed_event(11, to_text=NARROW, policy_id="ospol_other")
    event["payload"]["change_kind"] = "assigned"
    event["payload"]["target_type"] = "agent"
    event["payload"]["target_id"] = "agent-1"
    client.events = [event]

    assert instance._openshell_policy_gate() is not None


def test_a_change_already_on_disk_does_not_hold(mac_home, tmp_path):
    """The periodic sync can beat the announcement; that is convergence, not drift."""
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, NARROW, client)
    client.events = [_changed_event(11, to_text=NARROW)]

    assert instance._openshell_policy_gate() is None


def test_the_gate_can_be_switched_off(mac_home, tmp_path, monkeypatch):
    monkeypatch.setenv("MAC_OPENSHELL_POLICY_BUS_GATE", "0")
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)
    client.events = [_changed_event(11, to_text=NARROW)]

    assert instance._openshell_policy_gate() is None
    assert client.broadcast_reads == 0


def test_an_unreachable_hub_does_not_hold_the_worker(mac_home, tmp_path, monkeypatch):
    """Fail open on the READ: a hub outage already stops dispatch by itself.

    Holding here too would turn every hub blip into a second, longer stall for
    a policy change that may not exist.
    """
    client = _Client()
    instance = _worker(tmp_path, client)
    _converged(instance, mac_home, WIDE, client)

    def _boom(_path):
        raise RuntimeError("hub down")

    monkeypatch.setattr(client, "get", _boom)

    assert instance._openshell_policy_gate() is None
