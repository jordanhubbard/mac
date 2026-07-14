"""Worker attestation-key self-heal (2026-07-14 churn root cause).

A hub-side key rotation under a LIVE worker (e.g. a gateway deploy re-keying the
agent) left the worker signing every verification manifest with a stale key for
22 hours — every finished task was rejected at submit-for-review ("signature
does not verify"), blocked, retried the same deterministic failure, and died at
max attempts. Startup-only key validation cannot catch a mid-run rotation;
_heal_attestation_key recovers in place: verify against the hub, rotate/adopt/
persist when stale, so the retry signs correctly.
"""
from __future__ import annotations

from pathlib import Path

from mac.worker import MacWorker, WorkerExecution


class _HealClient:
    """Scripted hub: attestation verify + rotate endpoints."""

    def __init__(self, key_valid: bool, new_key: str = "K2-new") -> None:
        self.key_valid = key_valid
        self.new_key = new_key
        self.rotations = 0
        self.calls: list[str] = []

    def get(self, path: str):
        self.calls.append("GET " + path)
        return {}

    def post(self, path: str, body):
        self.calls.append("POST " + path)
        if path.endswith("/attestation-key/verify"):
            return {"valid": self.key_valid}
        if path.endswith("/attestation-key/rotate"):
            self.rotations += 1
            return {"attestation_key": self.new_key}
        return {}


def _worker(tmp_path: Path, client, key="K1-stale", env_path=None) -> MacWorker:
    return MacWorker(
        client,
        "agent_w",
        tmp_path,
        lambda _t, _d: WorkerExecution(0, "unused"),
        attestation_key=key,
        attestation_key_env_path=env_path,
    )


def test_heal_rotates_adopts_and_persists_when_hub_key_differs(tmp_path, monkeypatch):
    monkeypatch.delenv("MAC_ATTESTATION_KEY", raising=False)
    env_file = tmp_path / "mac.env"
    env_file.write_text("MAC_ATTESTATION_KEY=K1-stale\n", encoding="utf-8")
    client = _HealClient(key_valid=False)
    w = _worker(tmp_path, client, env_path=env_file)

    assert w._heal_attestation_key() is True
    assert client.rotations == 1
    assert w.attestation_key == "K2-new"          # adopted in-process
    assert "K2-new" in env_file.read_text(encoding="utf-8")  # persisted for restarts


def test_heal_refuses_to_rotate_a_valid_key(tmp_path, monkeypatch):
    monkeypatch.delenv("MAC_ATTESTATION_KEY", raising=False)
    client = _HealClient(key_valid=True)
    w = _worker(tmp_path, client)
    assert w._heal_attestation_key() is False
    assert client.rotations == 0                   # a good key is never rotated
    assert w.attestation_key == "K1-stale"


def test_heal_is_rate_limited(tmp_path, monkeypatch):
    monkeypatch.delenv("MAC_ATTESTATION_KEY", raising=False)
    monkeypatch.setenv("MAC_ATTESTATION_HEAL_MIN_SECONDS", "3600")
    client = _HealClient(key_valid=False)
    w = _worker(tmp_path, client)
    assert w._heal_attestation_key() is True
    # a second rejection within the window must NOT rotate again
    assert w._heal_attestation_key() is False
    assert client.rotations == 1


def test_heal_without_a_key_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("MAC_ATTESTATION_KEY", raising=False)
    client = _HealClient(key_valid=False)
    w = _worker(tmp_path, client, key=None)
    assert w._heal_attestation_key() is False
    assert client.rotations == 0
