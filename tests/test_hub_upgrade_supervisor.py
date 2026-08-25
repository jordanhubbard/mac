from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac.hub_upgrade_supervisor import (
    HubUpgradeSupervisor,
    MANIFEST_SCHEMA,
    SupervisorError,
)


SHA = "a" * 40
ROOT = Path(__file__).resolve().parents[1]


class FakeServices:
    def __init__(self) -> None:
        self.calls = []

    def stop(self, service: str) -> None:
        self.calls.append(("stop", service))

    def start(self, service: str) -> None:
        self.calls.append(("start", service))


def _fixture(tmp_path: Path):
    home = tmp_path / "mac"
    generations = home / "generations"
    old_source = generations / "old-source"
    old_venv = generations / "old-venv"
    new_source = generations / "new-source"
    new_venv = generations / "new-venv"
    for path in (old_source, old_venv, new_source, new_venv):
        path.mkdir(parents=True)
    (new_venv / "bin").mkdir()
    (new_venv / "bin/python").write_text("")
    current = home / "current"
    current.mkdir()
    (current / "source").symlink_to(old_source)
    (current / "venv").symlink_to(old_venv)
    manifest = {
        "schema": MANIFEST_SCHEMA,
        "transaction_id": "upgrade-test",
        "generation_id": "generation-new",
        "authorization": {
            "status": "authorized",
            "human_id": "human_alice",
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        },
        "source_link": str(current / "source"),
        "venv_link": str(current / "venv"),
        "staged_source": str(new_source),
        "staged_venv": str(new_venv),
        "service": "com.mac.control-plane",
        "health_url": "http://127.0.0.1:8789/health",
        "attestation_url": "http://127.0.0.1:8789/startup-attestation",
        "expected_commit_sha": SHA,
        "required_health_successes": 2,
        "health_timeout_seconds": 5,
    }
    manifest_path = home / "upgrades" / "handoffs" / "upgrade-test.json"
    manifest_path.parent.mkdir(parents=True)
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    manifest_path.write_bytes(raw)
    manifest_path.chmod(0o600)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    services = FakeServices()
    supervisor = HubUpgradeSupervisor(
        mac_home=home,
        service_controller=services,
        sleep=lambda _seconds: None,
    )
    supervisor._require_generation = lambda *_args: None
    return supervisor, services, manifest_path, digest, current, old_source, new_source


def test_supervisor_swaps_generation_only_after_digest_bound_authorization(tmp_path: Path):
    supervisor, services, manifest, digest, current, _old, new = _fixture(tmp_path)
    supervisor._prove_health = lambda _manifest: {
        "consecutive_successes": 3,
        "proved_at": "now",
    }

    receipt = supervisor.apply(manifest, digest)

    assert (current / "source").resolve() == new.resolve()
    assert services.calls == [
        ("stop", "com.mac.control-plane"),
        ("start", "com.mac.control-plane"),
    ]
    assert receipt["status"] == "committed"
    assert receipt["expected_commit_sha"] == SHA
    assert receipt["receipt_digest"].startswith("sha256:")


def test_supervisor_rejects_changed_manifest(tmp_path: Path):
    supervisor, _services, manifest, digest, _current, _old, _new = _fixture(tmp_path)
    manifest.write_text(manifest.read_text() + " ")

    with pytest.raises(SupervisorError, match="digest mismatch"):
        supervisor.apply(manifest, digest)


def test_failed_hub_health_restores_prior_generation(tmp_path: Path):
    supervisor, services, manifest, digest, current, old, _new = _fixture(tmp_path)
    supervisor._prove_health = lambda _manifest: (_ for _ in ()).throw(
        SupervisorError("wrong startup attestation")
    )
    supervisor._source_commit = lambda _source: "b" * 40

    with pytest.raises(SupervisorError, match="rolled back"):
        supervisor.apply(manifest, digest)

    assert (current / "source").resolve() == old.resolve()
    assert services.calls[-2:] == [
        ("stop", "com.mac.control-plane"),
        ("start", "com.mac.control-plane"),
    ]


def test_crash_recovery_uses_only_manifest_and_durable_state(tmp_path: Path):
    supervisor, _services, manifest, digest, current, old, new = _fixture(tmp_path)
    state = {
        "schema": "mac.hub_upgrade_receipt.v1",
        "transaction_id": "upgrade-test",
        "manifest_digest": digest,
        "phase": "swapped",
        "previous_source_target": str(old),
        "previous_venv_target": str((old.parent / "old-venv")),
        "supervisor_pid": 99999999,
    }
    state_path = supervisor._state_path("upgrade-test")
    supervisor._write_state(state_path, state)
    supervisor._atomic_link(current / "source", new)
    supervisor._source_commit = lambda _source: "b" * 40

    recovered = supervisor.recover_all()

    assert recovered["recovered"] == ["upgrade-test"]
    assert (current / "source").resolve() == old.resolve()


def test_legacy_deploy_remains_a_compatible_generation_path() -> None:
    installer = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    assert 'candidate_commit="$(git -C "$HOME/.mac/current/source" rev-parse HEAD' in installer
    assert '[ "$candidate_commit" = "$attested_commit" ]' in installer
    assert 'runtime_source="$HOME/.mac/src/mac"' in installer
    assert 'MAC_SOURCE_COMMIT="$(git -C "$runtime_source" rev-parse HEAD' in installer
