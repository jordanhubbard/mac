from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi.testclient import TestClient

import mac.fleet_upgrade_service as fleet_upgrade_module
from mac.api import TokenPrincipal, create_app
from mac.hub_upgrade_supervisor import RECEIPT_SCHEMA
from mac.services import ControlPlane
from mac.source_release_gate import StagedSourceRelease


SHA = "c" * 40
DIGEST = "sha256:" + "d" * 64


class FakeGate:
    def __init__(self, stage: Path) -> None:
        self.stage = stage

    def stage_approved_current(self, **_kwargs) -> StagedSourceRelease:
        return StagedSourceRelease(
            repository_name="mac",
            canonical_remote_url="https://github.com/example/mac.git",
            branch="main",
            commit_sha=SHA,
            canonical_ref=SHA,
            tree_digest=DIGEST,
            stage_path=str(self.stage),
            evidence={
                "schema": "mac.source_release_gate.v1",
                "commit_sha": SHA,
                "ci": {
                    "known": True,
                    "passed": ["contract"],
                    "pending": [],
                    "failed": [],
                },
                "local_contract_tests": {"status": "passed"},
            },
            evidence_digest="sha256:" + "e" * 64,
        )


def _fixture(tmp_path: Path, *, recovery_policy: str = "retain-upgraded-hub"):
    cp = ControlPlane.in_memory()
    human = cp.register_human(username="alice")
    machine = cp.register_machine("worker-host")
    worker = cp.register_agent(machine.id, "worker")
    fleet = cp.create_fleet("primary", agent_ids=[worker.id])
    home = tmp_path / "mac-home"
    stage = home / "upgrades/staging/upgrade"
    (stage / ".venv/bin").mkdir(parents=True)
    (stage / ".venv/bin/python").write_text("")
    old_source = home / "generations/old-source"
    old_venv = home / "generations/old-venv"
    old_source.mkdir(parents=True)
    old_venv.mkdir()
    current = home / "current"
    current.mkdir()
    (current / "source").symlink_to(old_source)
    (current / "venv").symlink_to(old_venv)
    cp.fleet_upgrades.mac_home = home
    cp.fleet_upgrades.source_gate = FakeGate(stage)
    upgrade = cp.request_fleet_upgrade(
        fleet_id=fleet.id,
        requested_by_human=human.id,
        requested_by_principal="client_alice",
        idempotency_key="slack:T1:C1:1.000",
        target_policy="approved-current",
        reason="roll forward to the reviewed fix",
        slack_provenance={
            "workspace_id": "T1",
            "channel_id": "C1",
            "message_ts": "1.000",
        },
        recovery_policy=recovery_policy,
    )
    return cp, human, fleet, upgrade


def _stage_and_arm(cp: ControlPlane, upgrade_id: str):
    cp.stage_fleet_upgrade(upgrade_id, actor="human_alice")
    return cp.arm_fleet_upgrade(
        upgrade_id,
        actor="human_alice",
        service="com.mac.control-plane",
        health_url="http://127.0.0.1:8789/health",
        attestation_url="http://127.0.0.1:8789/startup-attestation",
    )


def test_request_is_idempotent_and_binds_human_identity(tmp_path: Path):
    cp, human, fleet, first = _fixture(tmp_path)

    retry = cp.request_fleet_upgrade(
        fleet_id=fleet.id,
        requested_by_human=human.id,
        requested_by_principal="client_alice",
        idempotency_key="slack:T1:C1:1.000",
        target_policy="approved-current",
        reason="roll forward to the reviewed fix",
        slack_provenance={
            "workspace_id": "T1",
            "channel_id": "C1",
            "message_ts": "1.000",
        },
    )

    assert retry["id"] == first["id"]
    assert retry["requested_by_human"] == human.id


def test_upgrade_api_rejects_agent_and_unprivileged_tokens(tmp_path: Path):
    cp = ControlPlane.in_memory()
    alice = cp.register_human(username="alice")
    bob = cp.register_human(username="bob")
    fleet = cp.create_fleet("primary")
    app = create_app(
        control_plane=cp,
        auth_tokens={
            "alice": TokenPrincipal(scopes=frozenset({"upgrade"}), human_id=alice.id),
            "writer": TokenPrincipal(scopes=frozenset({"write"}), human_id=alice.id),
            "agent": TokenPrincipal(
                scopes=frozenset({"upgrade"}),
                human_id=alice.id,
                agent_id="agent_openclaw",
            ),
        },
    )
    payload = {
        "fleet_id": fleet.id,
        "idempotency_key": "slack-request-123",
        "target_policy": "approved-current",
        "reason": "apply current approved code",
        # Ignored even if a caller tries to assert another identity.
        "requested_by_human": bob.id,
    }

    writer = TestClient(app).post(
        "/fleet-upgrades",
        headers={"Authorization": "Bearer writer"},
        json=payload,
    )
    agent = TestClient(app).post(
        "/fleet-upgrades",
        headers={"Authorization": "Bearer agent"},
        json=payload,
    )
    accepted = TestClient(app).post(
        "/fleet-upgrades",
        headers={"Authorization": "Bearer alice"},
        json=payload,
    )

    assert writer.status_code == 403
    assert agent.status_code == 403
    assert accepted.status_code in {200, 201}, accepted.text
    assert accepted.json()["requested_by_human"] == alice.id


def test_supervisor_receipt_resumes_transaction_and_publishes_release(tmp_path: Path):
    cp, _human, _fleet, upgrade = _fixture(tmp_path)
    armed = _stage_and_arm(cp, upgrade["id"])
    release = cp.get_source_release(armed["resolved_release_id"])
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "transaction_id": upgrade["id"],
        "manifest_digest": armed["handoff_digest"],
        "generation_id": armed["generation_id"],
        "expected_commit_sha": release.commit_sha,
        "status": "committed",
    }
    receipt["receipt_digest"] = cp.fleet_upgrades._digest(receipt)

    resumed = cp.record_fleet_upgrade_supervisor_receipt(
        upgrade["id"],
        receipt=receipt,
        actor="hub-startup",
    )

    assert resumed["state"] == "hub_committed"
    assert resumed["phase"] == "hub_health_proved"
    assert cp.get_source_release(release.id).status == "published"


def test_failed_hub_swap_resume_clears_only_upgrade_owned_worker_holds(tmp_path: Path):
    cp, _human, _fleet, upgrade = _fixture(tmp_path)
    armed = _stage_and_arm(cp, upgrade["id"])
    held = next(agent for agent in cp.list_agents() if agent.name == "worker")
    assert held.dispatch_hold_reason == "fleet_upgrade:%s:hub_cutover" % upgrade["id"]
    state_path = (
        cp.fleet_upgrades.mac_home / "upgrades" / "supervisor" / ("%s.state.json" % upgrade["id"])
    )
    state_path.parent.mkdir(parents=True)
    state_path.write_text(
        '{"phase":"rolled_back","error":"new hub failed health"}\n',
        encoding="utf-8",
    )

    resumed = cp.resume_fleet_upgrades(actor="hub-startup")

    assert resumed[0]["state"] == "rolled_back"
    assert cp.get_agent(held.id).dispatch_hold is False
    assert armed["handoff_digest"]


def test_upgrade_arm_skips_operator_held_and_virtual_agents(tmp_path: Path):
    cp, _human, fleet, upgrade = _fixture(tmp_path)
    worker = next(agent for agent in cp.list_agents() if agent.name == "worker")
    session = cp.register_agent(cp.register_machine("laptop").id, "cursor-session")
    operator = cp.register_agent(
        cp.register_machine("hub-host").id,
        "operator",
        resources={"virtual": True},
    )
    cp.set_agent_dispatch_hold(session.id, "interactive session")
    cp.update_fleet(
        fleet.id,
        agent_ids=[worker.id, session.id, operator.id],
        actor="human_alice",
    )

    armed = _stage_and_arm(cp, upgrade["id"])

    fenced = next(
        event["detail"]["fenced_agents"]
        for event in cp.fleet_upgrade_events(upgrade["id"])
        if event["phase"] == "hub_swap_armed"
    )
    assert fenced == [worker.id]
    assert armed["state"] == "hub_applying"
    assert cp.get_agent(worker.id).dispatch_hold_reason == "fleet_upgrade:%s:hub_cutover" % upgrade[
        "id"
    ]
    assert cp.get_agent(session.id).dispatch_hold_reason == "interactive session"
    assert cp.get_agent(operator.id).dispatch_hold is False


def test_supervisor_launch_escapes_hub_systemd_cgroup(tmp_path: Path, monkeypatch):
    cp, _human, _fleet, upgrade = _fixture(tmp_path)
    _stage_and_arm(cp, upgrade["id"])
    executable = (
        cp.fleet_upgrades.mac_home / "current" / "venv" / "bin" / "mac-hub-upgrade-supervisor"
    )
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    launched = []

    def fake_run(argv, **_kwargs):
        launched.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="queued\n", stderr="")

    monkeypatch.setenv("MAC_HUB_SELF_UPGRADE_ENABLED", "1")
    monkeypatch.setattr(fleet_upgrade_module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(fleet_upgrade_module.subprocess, "run", fake_run)

    result = cp.launch_fleet_upgrade_hub_swap(upgrade["id"], actor="deployer")

    suffix = fleet_upgrade_module.hashlib.sha256(upgrade["id"].encode()).hexdigest()[:16]
    assert result["state"] == "hub_applying"
    assert launched[0][:4] == [
        "sudo",
        "-n",
        "systemd-run",
        "--unit=mac-hub-upgrade-%s" % suffix,
    ]
    assert "--uid=%d" % fleet_upgrade_module.os.getuid() in launched[0]
    assert "--no-block" in launched[0]


def test_worker_epoch_commit_sets_desired_source_after_hub_restart(tmp_path: Path, monkeypatch):
    cp, _human, _fleet, upgrade = _fixture(tmp_path)
    armed = _stage_and_arm(cp, upgrade["id"])
    release = cp.get_source_release(armed["resolved_release_id"])
    receipt = {
        "schema": RECEIPT_SCHEMA,
        "transaction_id": upgrade["id"],
        "manifest_digest": armed["handoff_digest"],
        "generation_id": armed["generation_id"],
        "expected_commit_sha": release.commit_sha,
        "status": "committed",
    }
    receipt["receipt_digest"] = cp.fleet_upgrades._digest(receipt)
    cp.record_fleet_upgrade_supervisor_receipt(upgrade["id"], receipt=receipt, actor="hub-startup")
    cp.store.execute(
        """
        UPDATE fleet_upgrades
        SET state = 'workers_proved', phase = 'worker_epoch_proved',
            epoch_id = NULL, epoch_identity_sha256 = 'identity'
        WHERE id = ?
        """,
        (upgrade["id"],),
    )
    monkeypatch.setattr(cp.fleet_release_epochs, "commit", lambda *_args, **_kwargs: {})

    completed = cp.commit_fleet_upgrade_epoch(upgrade["id"], actor="hub-upgrade")

    assert completed["state"] == "completed"
    assert completed["desired_source_state_id"]


def test_cohort_abort_returns_digest_bound_hub_rollback_when_policy_requires(
    tmp_path: Path, monkeypatch
):
    cp, _human, _fleet, upgrade = _fixture(
        tmp_path,
        recovery_policy="rollback-hub-on-cohort-failure",
    )
    armed = _stage_and_arm(cp, upgrade["id"])
    cp.store.execute(
        """
        UPDATE fleet_upgrades
        SET state = 'workers_open', phase = 'worker_epoch_open',
            epoch_id = NULL, epoch_identity_sha256 = 'identity'
        WHERE id = ?
        """,
        (upgrade["id"],),
    )
    monkeypatch.setattr(cp.fleet_release_epochs, "abort", lambda *_args, **_kwargs: {})

    aborted = cp.abort_fleet_upgrade_epoch(
        upgrade["id"],
        actor="hub-upgrade",
        reason="worker health failed",
    )

    assert aborted["state"] == "hub_rollback_required"
    assert aborted["supervisor"]["argv"][-1] == armed["handoff_digest"]
