import json

import pytest

from mac.judgement import JudgementConfig, JudgementProcess
from mac.models import HealthStatus, ValidationError
from mac.services import (
    DEFAULT_HUB_REVIEWER_AGENT_ID,
    DEFAULT_HUB_REVIEWER_MACHINE_ID,
    HUB_REVIEWER_KEY_RECOVERY_LIMIT,
    HUB_REVIEWER_KEY_RESOURCE_KEY,
    HUB_REVIEWER_KEY_STATUS_SCHEMA,
    HUB_REVIEW_VERIFIER_RESOURCE_SCHEMA,
    ControlPlane,
)


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _register_virtual_reviewer(cp):
    machine = cp.register_machine(
        "operator-review",
        machine_id=DEFAULT_HUB_REVIEWER_MACHINE_ID,
        resources={"virtual": True},
        trusted=True,
    )
    return cp.register_agent(
        machine.id,
        "hub-reviewer",
        capabilities=["review"],
        resources={
            "virtual": True,
            "hub_review_verifier": {
                "schema": HUB_REVIEW_VERIFIER_RESOURCE_SCHEMA,
                "enabled": True,
                "mode": "hub_verify",
            },
        },
        agent_id=DEFAULT_HUB_REVIEWER_AGENT_ID,
    )


@pytest.mark.parametrize(
    ("ciphertext", "expected"),
    [(None, "absent"), ("not-fernet-ciphertext", "undecryptable")],
)
def test_status_distinguishes_absent_from_undecryptable_without_exposing_secret(
    cp, ciphertext, expected
):
    reviewer = _register_virtual_reviewer(cp)
    cp.store.execute(
        "UPDATE agents SET attestation_key_ciphertext = ? WHERE id = ?",
        (ciphertext, reviewer.id),
    )

    status = cp.hub_reviewer_attestation_key_status(reviewer.id)

    assert status == {
        "schema": HUB_REVIEWER_KEY_STATUS_SCHEMA,
        "agent_id": reviewer.id,
        "state": expected,
        "ready": False,
        "recovery_attempts": 0,
        "recovery_limit": HUB_REVIEWER_KEY_RECOVERY_LIMIT,
    }
    assert "key" not in status
    assert "ciphertext" not in status


@pytest.mark.parametrize("broken", [None, "not-fernet-ciphertext"])
def test_virtual_reviewer_key_self_heals_once_and_is_idempotent(cp, broken):
    reviewer = _register_virtual_reviewer(cp)
    cp.store.execute(
        "UPDATE agents SET attestation_key_ciphertext = ? WHERE id = ?",
        (broken, reviewer.id),
    )

    first = cp.ensure_hub_reviewer_attestation_key(reviewer.id, actor="test")
    row = cp.store.query_one(
        "SELECT attestation_key_ciphertext FROM agents WHERE id = ?", (reviewer.id,)
    )
    first_ciphertext = row["attestation_key_ciphertext"]
    second = cp.ensure_hub_reviewer_attestation_key(reviewer.id, actor="test")

    assert first == second
    assert (
        cp.store.query_one(
            "SELECT attestation_key_ciphertext FROM agents WHERE id = ?", (reviewer.id,)
        )["attestation_key_ciphertext"]
        == first_ciphertext
    )
    status = cp.hub_reviewer_attestation_key_status(reviewer.id)
    assert status["state"] == "decryptable"
    assert status["ready"] is True
    assert status["recovery_attempts"] == 1
    assert cp.get_agent(reviewer.id).health_status == HealthStatus.HEALTHY.value
    events = cp.store.query_all(
        "SELECT event_type, detail FROM agent_lifecycle_events "
        "WHERE agent_id = ? AND event_type = ?",
        (reviewer.id, "agent.virtual_reviewer_attestation_key.recovered"),
    )
    assert len(events) == 1
    assert json.loads(events[0]["detail"])["previous_state"] == (
        "absent" if broken is None else "undecryptable"
    )


def test_virtual_reviewer_key_recovery_is_bounded_and_explicitly_unhealthy(cp, monkeypatch):
    reviewer = _register_virtual_reviewer(cp)
    cp.store.execute(
        "UPDATE agents SET attestation_key_ciphertext = NULL WHERE id = ?",
        (reviewer.id,),
    )
    calls = []

    def fail_encrypt(_value):
        calls.append(True)
        raise RuntimeError("synthetic encryption failure")

    monkeypatch.setattr(cp.secrets, "_encrypt", fail_encrypt)

    for _ in range(HUB_REVIEWER_KEY_RECOVERY_LIMIT):
        with pytest.raises(ValidationError, match="recovery failed"):
            cp.ensure_hub_reviewer_attestation_key(reviewer.id, actor="test")
    with pytest.raises(ValidationError, match="recovery exhausted"):
        cp.ensure_hub_reviewer_attestation_key(reviewer.id, actor="test")

    assert len(calls) == HUB_REVIEWER_KEY_RECOVERY_LIMIT
    status = cp.hub_reviewer_attestation_key_status(reviewer.id)
    assert status["state"] == "absent"
    assert status["ready"] is False
    assert status["recovery_attempts"] == HUB_REVIEWER_KEY_RECOVERY_LIMIT
    assert cp.get_agent(reviewer.id).health_status == HealthStatus.UNHEALTHY.value
    resource = cp.get_agent(reviewer.id).resources[HUB_REVIEWER_KEY_RESOURCE_KEY]
    assert resource["recovery_exhausted"] is True
    exhausted = cp.store.query_one(
        "SELECT COUNT(*) AS count FROM agent_lifecycle_events "
        "WHERE agent_id = ? AND event_type = ?",
        (reviewer.id, "agent.virtual_reviewer_attestation_key.recovery_exhausted"),
    )
    assert exhausted["count"] == 1


def test_judgement_reports_registered_reviewer_key_failure(cp):
    reviewer = _register_virtual_reviewer(cp)
    cp.store.execute(
        "UPDATE agents SET attestation_key_ciphertext = ? WHERE id = ?",
        ("not-fernet-ciphertext", reviewer.id),
    )

    report = JudgementProcess(cp, JudgementConfig(enabled=True)).run_once()

    finding = next(
        item
        for item in report["findings"]
        if item["kind"] == "hub_reviewer_attestation_key_unhealthy"
    )
    assert finding["agent_id"] == reviewer.id
    assert finding["detail"]["state"] == "undecryptable"
    assert finding["detail"]["ready"] is False
    assert report["actions"] == [
        {
            "action": "skipped",
            "reason": "no_recommended_action",
            "finding_kind": "hub_reviewer_attestation_key_unhealthy",
            "task_id": "",
        }
    ]
