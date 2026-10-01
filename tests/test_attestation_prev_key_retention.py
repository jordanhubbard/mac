"""An attestation key rotation keeps the previous key for signatures made under it."""

from __future__ import annotations

import pytest

from mac.services import ControlPlane


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


def _register_agent(cp, name, capabilities=None):
    capabilities = capabilities or []
    resources = {}
    if "python" in capabilities:
        resources["commands"] = {
            "schema": "mac.command_inventory.v1",
            "available": ["python3", "git", "gh"],
        }
    machine = cp.register_machine("%s-host" % name, resources={"cpu": 4, "memory_gb": 8})
    return cp.register_agent(machine.id, name, capabilities=capabilities, resources=resources)


def test_attestation_rotation_retains_prev_key_for_pre_rotation_verdicts(cp):
    """Rotation retains the previous key and the verifier accepts a pre-rotation
    signature against the key that was active at signing time; the retention is
    bounded to a single previous key."""
    from mac.services import (
        sign_verification_manifest,
        verify_verification_manifest_signature,
    )

    agent = _register_agent(cp, "reviewer-rot", capabilities=["python"])
    key1 = cp._agent_attestation_key(agent.id)
    assert key1

    manifest = {
        "schema": "mac.worker_evidence.v1",
        "evidence_type": "review_verdict",
        "status": "complete",
        "nonce": "abc123",
    }
    sig = sign_verification_manifest(key1, manifest)
    assert verify_verification_manifest_signature(key1, manifest, sig)

    # Rotate: the current key changes and the previous key is retained.
    key2 = cp.rotate_agent_attestation_key(agent.id)
    assert key2 and key2 != key1
    assert cp._agent_attestation_key(agent.id) == key2
    assert cp._agent_attestation_prev_key(agent.id) == key1

    # The pre-rotation signature no longer verifies against the CURRENT key,
    # but DOES verify against the retained previous (signing-time) key.
    assert not verify_verification_manifest_signature(key2, manifest, sig)
    assert verify_verification_manifest_signature(
        cp._agent_attestation_prev_key(agent.id), manifest, sig
    )

    # Bounded to a single previous key: a second rotation drops key1.
    key3 = cp.rotate_agent_attestation_key(agent.id)
    assert key3 and key3 not in (key1, key2)
    assert cp._agent_attestation_prev_key(agent.id) == key2
    assert cp._agent_attestation_prev_key(agent.id) != key1
