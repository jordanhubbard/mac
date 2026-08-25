from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import mac.hub_upgrade_supervisor as supervisor_module
from mac.hub_upgrade_supervisor import (
    HostServiceController,
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


def _rewrite_manifest(path: Path, update) -> str:
    document = json.loads(path.read_text(encoding="utf-8"))
    update(document)
    raw = (json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return "sha256:" + hashlib.sha256(raw).hexdigest()


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


def test_committed_apply_is_idempotent_and_explicit_rollback_restores_previous(
    tmp_path: Path,
) -> None:
    supervisor, services, manifest, digest, current, old, new = _fixture(tmp_path)
    supervisor._prove_health = lambda _manifest: {"consecutive_successes": 2}
    first = supervisor.apply(manifest, digest)

    assert supervisor.apply(manifest, digest) == first
    assert (current / "source").resolve() == new.resolve()

    supervisor._source_commit = lambda _source: "b" * 40
    status = supervisor.rollback(manifest, digest)

    assert status["phase"] == "rolled_back"
    assert status["receipt"]["status"] == "committed"
    assert (current / "source").resolve() == old.resolve()
    assert services.calls[-2:] == [
        ("stop", "com.mac.control-plane"),
        ("start", "com.mac.control-plane"),
    ]


def test_recover_restores_an_interrupted_transaction(tmp_path: Path) -> None:
    supervisor, _services, manifest, digest, current, old, new = _fixture(tmp_path)
    supervisor._write_state(
        supervisor._state_path("upgrade-test"),
        {
            "transaction_id": "upgrade-test",
            "manifest_digest": digest,
            "phase": "started",
            "previous_source_target": str(old),
            "previous_venv_target": str(old.parent / "old-venv"),
        },
    )
    supervisor._atomic_link(current / "source", new)
    supervisor._source_commit = lambda _source: "b" * 40

    status = supervisor.recover(manifest, digest)

    assert status["phase"] == "rolled_back"
    assert status["receipt"] is None
    assert (current / "source").resolve() == old.resolve()
    assert supervisor.recover(manifest, digest)["phase"] == "rolled_back"


def test_recover_all_handles_empty_terminal_receipted_and_active_states(
    tmp_path: Path,
) -> None:
    supervisor, _services, _manifest, digest, _current, old, _new = _fixture(tmp_path)
    assert HubUpgradeSupervisor(mac_home=tmp_path / "empty").recover_all() == {
        "schema": "mac.hub_upgrade_receipt.v1",
        "recovered": [],
        "skipped_active": [],
    }
    for transaction_id, phase, pid in (
        ("done", "committed", 0),
        ("receipted", "started", 0),
        ("active", "swapped", os.getpid()),
    ):
        supervisor._write_state(
            supervisor._state_path(transaction_id),
            {
                "transaction_id": transaction_id,
                "manifest_digest": digest,
                "phase": phase,
                "previous_source_target": str(old),
                "previous_venv_target": str(old.parent / "old-venv"),
                "supervisor_pid": pid,
            },
        )
    supervisor._write_private_json(
        supervisor._receipt_path("receipted"),
        {"manifest_digest": digest, "status": "committed"},
    )

    result = supervisor.recover_all()

    assert result["recovered"] == []
    assert result["skipped_active"] == ["active"]
    assert supervisor._read_json(supervisor._state_path("receipted"))["phase"] == "committed"


@pytest.mark.parametrize(
    ("update", "error"),
    (
        (lambda value: value.pop("service"), "missing required fields"),
        (lambda value: value.update(schema="wrong"), "unsupported handoff"),
        (
            lambda value: value.update(authorization={"status": "denied"}),
            "not authorized",
        ),
        (
            lambda value: value["authorization"].update(
                expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            ),
            "authorization expired",
        ),
        (lambda value: value.update(service="../unsafe"), "invalid service"),
    ),
)
def test_manifest_validation_rejects_untrusted_material(tmp_path: Path, update, error: str) -> None:
    supervisor, _services, manifest, _digest, _current, _old, _new = _fixture(tmp_path)
    digest = _rewrite_manifest(manifest, update)

    with pytest.raises(SupervisorError, match=error):
        supervisor.status(manifest, digest)


def test_supervisor_rejects_malformed_private_and_escaping_inputs(tmp_path: Path) -> None:
    supervisor, _services, manifest, _digest, _current, _old, _new = _fixture(tmp_path)
    manifest.write_text("{", encoding="utf-8")
    manifest.chmod(0o600)
    digest = "sha256:" + hashlib.sha256(manifest.read_bytes()).hexdigest()
    with pytest.raises(SupervisorError, match="not valid JSON"):
        supervisor.status(manifest, digest)

    manifest.chmod(0o644)
    with pytest.raises(SupervisorError, match="owner-private"):
        supervisor.status(manifest, digest)
    with pytest.raises(SupervisorError, match="escapes MAC_HOME"):
        supervisor._managed_path(tmp_path / "outside")
    with pytest.raises(SupervisorError, match="invalid transaction"):
        supervisor._state_path("../unsafe")

    malformed_state = supervisor.state_root / "malformed.json"
    supervisor._write_private_json(malformed_state, {"ok": True})
    malformed_state.write_text("[]", encoding="utf-8")
    with pytest.raises(SupervisorError, match="not an object"):
        supervisor._read_json(malformed_state)
    with pytest.raises(SupervisorError, match="state is missing"):
        supervisor._read_json(supervisor.state_root / "missing.json")
    with pytest.raises(SupervisorError, match="invalid authorization expiry"):
        supervisor._parse_time("not-a-time")
    assert supervisor._parse_time("2030-01-01T00:00:00").tzinfo == timezone.utc


def test_health_proof_requires_consecutive_matching_attestations(tmp_path: Path) -> None:
    supervisor, _services, manifest_path, _digest, _current, _old, _new = _fixture(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    responses = iter(
        [
            {"status": "bad"},
            {"source_commit": SHA, "generation_id": "generation-new"},
            {"status": "ok"},
            {"source_commit": SHA, "generation_id": "generation-new"},
            {"status": "ok"},
            {"source_commit": SHA, "generation_id": "generation-new"},
        ]
    )
    supervisor._fetch_json = lambda _url: next(responses)

    proof = supervisor._prove_health(manifest)

    assert proof["consecutive_successes"] == 2
    assert proof["health"] == {"status": "ok"}
    assert proof["attestation"]["source_commit"] == SHA


def test_generation_and_source_attestation_validate_git_results(
    tmp_path: Path, monkeypatch
) -> None:
    supervisor, _services, _manifest, _digest, _current, _old, new = _fixture(tmp_path)
    new_venv = new.parent / "new-venv"
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=SHA + "\n", stderr=""),
    )
    supervisor._require_generation = HubUpgradeSupervisor._require_generation.__get__(supervisor)

    supervisor._require_generation(new, new_venv, SHA)
    assert supervisor._source_commit(new) == SHA

    monkeypatch.setattr(
        supervisor_module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout="", stderr="bad git"),
    )
    with pytest.raises(SupervisorError, match="staged source"):
        supervisor._require_generation(new, new_venv, SHA)
    with pytest.raises(SupervisorError, match="could not attest"):
        supervisor._source_commit(new)


def test_host_service_controller_uses_fixed_platform_commands(monkeypatch) -> None:
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(supervisor_module.subprocess, "run", fake_run)
    darwin = HostServiceController(system="darwin")
    linux = HostServiceController(system="linux")
    darwin.stop("com.mac.control-plane")
    darwin.start("com.mac.control-plane")
    linux.stop("mac.service")
    linux.start("mac.service")

    assert [call[0] for call in calls] == [
        ["sudo", "-n", "launchctl", "kill", "SIGTERM", "system/com.mac.control-plane"],
        ["sudo", "-n", "launchctl", "kickstart", "-k", "system/com.mac.control-plane"],
        ["sudo", "-n", "systemctl", "stop", "mac.service"],
        ["sudo", "-n", "systemctl", "start", "mac.service"],
    ]
    with pytest.raises(SupervisorError, match="invalid service"):
        linux.start("../unsafe")
    with pytest.raises(SupervisorError, match="unsupported"):
        HostServiceController(system="plan9").start("mac.service")


def test_host_service_controller_surfaces_manager_failures(monkeypatch) -> None:
    monkeypatch.setattr(
        supervisor_module.subprocess,
        "run",
        lambda argv, **_kwargs: subprocess.CompletedProcess(argv, 1, "", "denied"),
    )
    with pytest.raises(SupervisorError, match="denied"):
        HostServiceController(system="linux").start("mac.service")


def test_supervisor_cli_dispatches_operations_and_reports_errors(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    calls = []

    class FakeSupervisor:
        def __init__(self, *, mac_home: Path) -> None:
            calls.append(("init", mac_home))

        def status(self, manifest: Path, digest: str):
            calls.append(("status", manifest, digest))
            return {"phase": "committed"}

        def rollback(self, manifest: Path, digest: str):
            raise SupervisorError("synthetic rollback refusal")

        def recover_all(self):
            calls.append(("recover-all",))
            return {"recovered": []}

    monkeypatch.setattr(supervisor_module, "HubUpgradeSupervisor", FakeSupervisor)
    home = tmp_path / "mac"
    manifest = tmp_path / "handoff.json"
    digest = "sha256:" + "a" * 64

    assert (
        supervisor_module.main(
            ["--mac-home", str(home), "status", str(manifest), "--digest", digest]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out) == {"phase": "committed"}
    assert supervisor_module.main(["--mac-home", str(home), "recover-all"]) == 0
    assert json.loads(capsys.readouterr().out) == {"recovered": []}
    assert (
        supervisor_module.main(
            ["--mac-home", str(home), "rollback", str(manifest), "--digest", digest]
        )
        == 1
    )
    assert "synthetic rollback refusal" in capsys.readouterr().err
    assert ("status", manifest, digest) in calls
    assert ("recover-all",) in calls
