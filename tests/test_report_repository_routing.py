from __future__ import annotations

from pathlib import Path
import os
import sys
import threading

import pytest

from mac import executor_sandbox as sandbox
from mac import models
from mac import worker
from mac import worker_subprocess
from mac.models import (
    REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY,
    REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY,
    REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY,
    AgentStatus,
    AuthorizationError,
    ReviewStatus,
    TaskState,
    ValidationError,
    agent_has_read_only_report_repository_executor,
    json_dumps,
    new_id,
    read_only_report_repository_executor_attestation,
    read_only_report_repository_executor_approval,
    read_only_report_repository_executor_resource,
    utcnow,
)
from mac.services import ControlPlane, sign_verification_manifest


_RUNTIME_REF = (
    "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "1" * 64
)
_POLICY_DIGEST = "sha256:" + "2" * 64
_TUPLE = {
    "openshell_bin_path": "/approved/openshell",
    "openshell_bin_sha256": "sha256:" + "3" * 64,
    "executor_path": "/approved/mac-task-executor",
    "executor_sha256": "sha256:" + "4" * 64,
    "platform": "linux",
    "isolation_posture": "landlock_enforced",
    "python_path": "/approved/python",
    "python_sha256": "sha256:" + "5" * 64,
    "executor_script_path": "/approved/mac-task-executor.py",
    "executor_script_sha256": "sha256:" + "6" * 64,
    "source_root": "/approved/mac",
    "source_bundle_sha256": "sha256:" + "7" * 64,
}


def _attestation():
    return read_only_report_repository_executor_attestation(
        runtime_image_ref=_RUNTIME_REF,
        policy_sha256=_POLICY_DIGEST,
        **_TUPLE,
    )


def _approval():
    return read_only_report_repository_executor_approval(
        runtime_image_ref=_RUNTIME_REF,
        policy_sha256=_POLICY_DIGEST,
        **_TUPLE,
    )


def _agent(cp, name, capabilities, *, attested, approved=True):
    machine = cp.register_machine(
        "%s-host" % name,
        resources={"cpu": 4, "memory_gb": 8},
    )
    resources = {
        "commands": {
            "schema": "mac.command_inventory.v1",
            "available": ["git", "gh", "python3"],
        }
    }
    if attested:
        resources.update(
            {
                "openshell_required": True,
                REPORT_REPOSITORY_EXECUTOR_ATTESTATION_KEY: _attestation(),
            }
        )
    agent = cp.register_agent(
        machine.id,
        name,
        capabilities=capabilities,
        resources=resources,
    )
    if attested and approved:
        updated = dict(agent.resources)
        updated[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY] = _approval()
        agent = cp.update_agent(agent.id, resources=updated, actor="test-admin")
    return agent


def _report_task(cp):
    return cp.create_task(
        "inspect repository without mutation",
        required_capabilities=["ops"],
        metadata={
            "deliverable": "report",
            "report_repository_access": {
                "schema": "mac.report_repository_access.v1",
                "mode": "read_only",
            },
            "execution_contract": {
                "type": "repository",
                "repository_contract": {
                    "schema": "mac.repository_contract.v1",
                    "project": "report-routing",
                    "canonical_remote_url": "https://example.invalid/report-routing.git",
                    "default_branch": "main",
                    "test": {"command": "true"},
                },
            },
        },
    )


def _signed_report_manifest(cp, agent_id):
    manifest = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "operator_result",
        "summary": "Repository analysis produced",
        "result": "Substantive findings and prioritized next work.",
        "repository_access": {
            "schema": "mac.report_repository_access.v1",
            "mode": "read_only",
        },
        "signed_by": agent_id,
    }
    manifest["signature"] = sign_verification_manifest(
        cp._agent_attestation_key(agent_id), manifest
    )
    return manifest


def test_hub_projects_exact_report_executor_marker_and_rejects_spoof():
    cp = ControlPlane.in_memory()
    spoofed = _agent(cp, "legacy", ["ops"], attested=False)
    spoofed_resources = dict(spoofed.resources)
    spoofed_resources["openshell_required"] = True
    spoofed_resources[REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY] = (
        read_only_report_repository_executor_resource(
            runtime_image_ref=_RUNTIME_REF,
            policy_sha256=_POLICY_DIGEST,
            **_TUPLE,
        )
    )
    spoofed = cp.heartbeat_agent(spoofed.id, resources=spoofed_resources)
    assert not agent_has_read_only_report_repository_executor(spoofed.resources)

    admitted = _agent(cp, "openshell", ["ops"], attested=True)
    assert admitted.resources[REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY] == (
        read_only_report_repository_executor_resource(
            runtime_image_ref=_RUNTIME_REF,
            policy_sha256=_POLICY_DIGEST,
            **_TUPLE,
        )
    )
    assert agent_has_read_only_report_repository_executor(admitted.resources)

    # A fresh inventory document without the worker attestation revokes the
    # derived marker; a stale controller marker cannot survive by echoing it.
    refreshed = cp.heartbeat_agent(
        admitted.id,
        resources={
            "commands": admitted.resources["commands"],
            REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY: admitted.resources[
                REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY
            ],
        },
    )
    assert not agent_has_read_only_report_repository_executor(refreshed.resources)


def test_worker_cannot_self_approve_report_executor():
    cp = ControlPlane.in_memory()
    worker_agent = _agent(
        cp, "unapproved-openshell", ["ops"], attested=True, approved=False
    )
    assert not agent_has_read_only_report_repository_executor(
        worker_agent.resources
    )

    forged = dict(worker_agent.resources)
    forged[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY] = _approval()
    forged[REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY] = (
        read_only_report_repository_executor_resource(
            runtime_image_ref=_RUNTIME_REF,
            policy_sha256=_POLICY_DIGEST,
            **_TUPLE,
        )
    )
    refreshed = cp.heartbeat_agent(worker_agent.id, resources=forged)
    assert REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY not in refreshed.resources
    assert not agent_has_read_only_report_repository_executor(refreshed.resources)
    with pytest.raises(ValidationError, match="report_repository_executor_missing"):
        cp.claim_task(_report_task(cp).id, worker_agent.id)


def test_stale_heartbeat_cannot_resurrect_revoked_report_approval(monkeypatch):
    cp = ControlPlane.in_memory()
    admitted = _agent(cp, "race-worker", ["ops"], attested=True)
    stale_resources = dict(admitted.resources)
    reached_stale_read = threading.Event()
    resume = threading.Event()
    original = cp._agent_resources_with_preserved_control_plane_fields
    paused_once = False

    def paused(agent_id, resources, *, conn=None):
        nonlocal paused_once
        result = original(agent_id, resources, conn=conn)
        if conn is None and not paused_once and threading.current_thread().name == "stale-heartbeat":
            paused_once = True
            reached_stale_read.set()
            assert resume.wait(5)
        return result

    monkeypatch.setattr(
        cp, "_agent_resources_with_preserved_control_plane_fields", paused
    )
    outcome = {}

    def heartbeat():
        outcome["agent"] = cp.heartbeat_agent(
            admitted.id, resources=stale_resources
        )

    thread = threading.Thread(target=heartbeat, name="stale-heartbeat")
    thread.start()
    assert reached_stale_read.wait(5)
    revoked = dict(cp.get_agent(admitted.id).resources)
    revoked[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY] = None
    cp.update_agent(admitted.id, resources=revoked, actor="test-admin")
    resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert "agent" in outcome
    assert REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY not in outcome["agent"].resources
    assert not agent_has_read_only_report_repository_executor(
        outcome["agent"].resources
    )


def test_report_claim_requires_marker_and_break_glass_cannot_bypass():
    cp = ControlPlane.in_memory()
    legacy = _agent(cp, "legacy", ["ops"], attested=False)
    task = _report_task(cp)

    assert cp._agent_availability_for_task(legacy, task) == (
        False,
        "report_repository_executor_missing",
    )
    with pytest.raises(ValidationError, match="report_repository_executor_missing"):
        cp.claim_task(task.id, legacy.id)
    with pytest.raises(ValidationError, match="cannot use host break-glass"):
        cp.authorize_task_break_glass(
            task.id,
            legacy.id,
            reason="attempt to bypass isolation",
            authorized_by="operator-test",
        )

    openshell = _agent(cp, "openshell", ["ops"], attested=True)
    claimed, _lease = cp.claim_task(task.id, openshell.id)
    assert claimed.owner_agent_id == openshell.id


def test_pending_k8s_review_is_retracted_and_nudged_to_attested_peer(
    monkeypatch,
):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    cp = ControlPlane.in_memory()
    executor = _agent(cp, "executor", ["ops", "review"], attested=True)
    k8s_reviewer = _agent(cp, "k8s-reviewer", ["review"], attested=False)
    openshell_reviewer = _agent(
        cp, "openshell-reviewer", ["review"], attested=True
    )
    task = _report_task(cp)
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://repository-report",
        "Repository analysis produced",
        executor.id,
        metadata={
            "returncode": 0,
            "verification": _signed_report_manifest(cp, executor.id),
        },
    )
    cp.submit_for_review(task.id, executor.id)
    assert cp.get_task(task.id).state == TaskState.NEEDS_REVIEW.value

    # Simulate a pending assignment made by the pre-cutover hub. The current
    # request_review API correctly refuses this reviewer, so the legacy row is
    # inserted directly as migration state.
    legacy_review_id = new_id("review")
    cp.store.execute(
        "INSERT INTO reviews "
        "(id, task_id, reviewer_agent_id, status, reason, evidence_id, "
        "created_at, completed_at) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)",
        (
            legacy_review_id,
            task.id,
            k8s_reviewer.id,
            ReviewStatus.PENDING.value,
            utcnow(),
        ),
    )

    result = cp.advance_default_review_workflow(task.id)

    assert result["status"] == "waiting_for_reviewer_verdict"
    assert result["reviewer_agent_id"] == openshell_reviewer.id
    assert result["executor_evidence_id"] == evidence.id
    assert result["nudge_id"]
    reviews = {review.id: review for review in cp.list_reviews(task.id)}
    assert reviews[legacy_review_id].status == ReviewStatus.RETRACTED.value
    assert "reviewer_report_repository_executor_missing" in (
        reviews[legacy_review_id].reason or ""
    )
    assert any(
        review.status == ReviewStatus.PENDING.value
        and review.reviewer_agent_id == openshell_reviewer.id
        for review in reviews.values()
    )


def test_review_claim_revalidates_report_marker_atomically(monkeypatch):
    monkeypatch.setenv("MAC_REVIEW_HUB_VERIFY", "0")
    cp = ControlPlane.in_memory()
    executor = _agent(cp, "claim-executor", ["ops", "review"], attested=True)
    reviewer = _agent(cp, "claim-reviewer", ["review"], attested=True)
    task = _report_task(cp)
    cp.claim_task(task.id, executor.id)
    cp.start_task(task.id, executor.id)
    evidence = cp.add_evidence(
        task.id,
        "log",
        "artifact://repository-report",
        "Repository analysis produced",
        executor.id,
        metadata={
            "returncode": 0,
            "verification": _signed_report_manifest(cp, executor.id),
        },
    )
    cp.submit_for_review(task.id, executor.id)
    advanced = cp.advance_default_review_workflow(task.id)
    assert advanced["reviewer_agent_id"] == reviewer.id
    pending = next(
        item
        for item in cp.list_reviews(task.id)
        if item.status == ReviewStatus.PENDING.value
    )

    revoked = dict(cp.get_agent(reviewer.id).resources)
    revoked[REPORT_REPOSITORY_EXECUTOR_APPROVAL_KEY] = None
    cp.update_agent(reviewer.id, resources=revoked, actor="test-admin")
    with pytest.raises(AuthorizationError, match="review assignment"):
        cp.claim_review(
            pending.id,
            reviewer.id,
            executor_evidence_id=evidence.id,
        )
    assert cp.get_agent(reviewer.id).status == AgentStatus.IDLE.value
    assert cp.get_agent(reviewer.id).current_task_id is None


def test_review_claim_merges_into_locked_policy_metadata(monkeypatch):
    cp = ControlPlane.in_memory()
    reviewer = _agent(cp, "policy-reviewer", ["review"], attested=True)
    task = cp.create_task("ordinary report", metadata={"deliverable": "report"})
    cp.store.execute(
        "UPDATE tasks SET state = ? WHERE id = ?",
        (TaskState.REVIEWING.value, task.id),
    )
    review_id = new_id("review")
    cp.store.execute(
        "INSERT INTO reviews "
        "(id, task_id, reviewer_agent_id, status, reason, evidence_id, "
        "created_at, completed_at) VALUES (?, ?, ?, ?, NULL, NULL, ?, NULL)",
        (
            review_id,
            task.id,
            reviewer.id,
            ReviewStatus.PENDING.value,
            utcnow(),
        ),
    )
    reached_prelock = threading.Event()
    resume = threading.Event()
    original = cp._review_claim_detail

    def paused(*args, **kwargs):
        detail = original(*args, **kwargs)
        reached_prelock.set()
        assert resume.wait(5)
        return detail

    monkeypatch.setattr(cp, "_review_claim_detail", paused)
    outcome = {}

    def claim():
        outcome["claim"] = cp.claim_review(review_id, reviewer.id)

    thread = threading.Thread(target=claim)
    thread.start()
    assert reached_prelock.wait(5)
    current = cp.get_task(task.id)
    changed = dict(current.metadata)
    changed["report_repository_access"] = {
        "schema": "mac.report_repository_access.v1",
        "mode": "read_only",
    }
    cp.store.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json_dumps(changed), task.id),
    )
    resume.set()
    thread.join(5)
    assert not thread.is_alive()
    assert outcome["claim"]["status"] == "claimed"
    assert cp.get_task(task.id).metadata["report_repository_access"] == changed[
        "report_repository_access"
    ]


@pytest.fixture()
def report_boundary_env(tmp_path: Path, monkeypatch):
    openshell = tmp_path / "openshell"
    openshell.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    openshell.chmod(0o755)
    executor = tmp_path / "mac-task-executor"
    executor.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executor.chmod(0o755)
    executor_script = tmp_path / "mac-task-executor.py"
    executor_script.write_text("raise SystemExit(0)\n", encoding="utf-8")
    source_root = tmp_path / "mac-source"
    (source_root / "src" / "mac").mkdir(parents=True)
    (source_root / "src" / "mac" / "__init__.py").write_text(
        "", encoding="utf-8"
    )
    (source_root / "pyproject.toml").write_text(
        "[project]\nname='mac-test'\n", encoding="utf-8"
    )
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\n", encoding="utf-8")
    runtime_ref = tmp_path / "runtime-image-ref"
    runtime_ref.write_text(_RUNTIME_REF + "\n", encoding="utf-8")
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX", "1")
    monkeypatch.setenv("MAC_OPENSHELL_BIN", str(openshell))
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(policy))
    monkeypatch.setenv("MAC_OPENSHELL_RUNTIME_IMAGE_REF_FILE", str(runtime_ref))
    monkeypatch.setenv("MAC_TASK_EXECUTOR_PYTHON", str(Path(sys.executable).resolve()))
    monkeypatch.setenv("MAC_TASK_EXECUTOR_SCRIPT", str(executor_script))
    monkeypatch.setenv("MAC_SELF_UPDATE_REPO", str(source_root))
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", "1")
    monkeypatch.delenv("MAC_OPENSHELL_KEEP", raising=False)
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX_NAME", raising=False)
    monkeypatch.delenv("MAC_EXECUTOR_BACKEND", raising=False)
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", "--from mutable-local-tag")
    # These boundary tests describe the *containerized* Linux runtime, which is
    # now the only platform that has one. Pin the platform so the suite asserts
    # the same thing on a macOS developer machine (where the node itself would
    # be a host install) as it does on a Linux CI runner.
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_kernel_has_landlock", lambda: True)
    return executor, policy


def test_worker_attests_digest_bound_per_task_report_executor(report_boundary_env):
    executor, policy = report_boundary_env
    attestation = worker._read_only_report_executor_attestation([str(executor)])
    assert attestation is not None
    assert attestation["runtime_image_ref"] == _RUNTIME_REF
    assert attestation["policy_sha256"] == (
        "sha256:" + __import__("hashlib").sha256(policy.read_bytes()).hexdigest()
    )
    assert attestation["executor_path"] == str(executor)
    assert attestation["source_root"].endswith("mac-source")
    assert sandbox._read_only_report_extra_create_argv(require_approval=False) == [
        "--from",
        _RUNTIME_REF,
    ]


def test_linux_report_attestation_requires_actual_landlock(
    report_boundary_env, monkeypatch
):
    executor, _policy = report_boundary_env
    monkeypatch.setattr(worker.sys, "platform", "linux")
    monkeypatch.setattr(sandbox, "_kernel_has_landlock", lambda: False)
    monkeypatch.setenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", "1")
    assert worker._read_only_report_executor_attestation([str(executor)]) is None

    monkeypatch.setattr(sandbox, "_kernel_has_landlock", lambda: True)
    attestation = worker._read_only_report_executor_attestation([str(executor)])
    assert attestation is not None
    assert (attestation["platform"], attestation["isolation_posture"]) == (
        "linux",
        "landlock_enforced",
    )


def test_darwin_host_install_attests_without_any_container(
    report_boundary_env, monkeypatch, tmp_path
):
    """A macOS host install -- no Docker, no OpenShell, no runtime image --
    still produces a valid attestation, under the honest ``macos_host``
    posture (ADR 0015). This is the case that could not pass at all while
    darwin required the Docker VM posture."""

    executor, _policy = report_boundary_env
    monkeypatch.setattr(worker.sys, "platform", "darwin")
    # Nothing container-shaped is available on this node.
    monkeypatch.delenv("MAC_OPENSHELL_ALLOW_NO_LANDLOCK", raising=False)
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX", raising=False)
    monkeypatch.setenv("MAC_OPENSHELL_BIN", str(tmp_path / "no-such-openshell"))
    monkeypatch.setenv(
        "MAC_OPENSHELL_RUNTIME_IMAGE_REF_FILE", str(tmp_path / "no-such-ref")
    )

    attestation = worker._read_only_report_executor_attestation([str(executor)])
    assert attestation is not None
    assert (attestation["platform"], attestation["isolation_posture"]) == (
        "darwin",
        "macos_host",
    )
    # The posture claims nothing that does not exist: no image, no policy,
    # no OpenShell binary.
    assert attestation["runtime_image_ref"] == ""
    assert attestation["policy_sha256"] == ""
    assert attestation["openshell_bin_path"] == ""
    assert attestation["openshell_bin_sha256"] == ""
    # What still exists on the host stays digest-bound.
    assert attestation["executor_path"] == str(executor)
    assert attestation["executor_sha256"].startswith("sha256:")
    assert attestation["source_bundle_sha256"].startswith("sha256:")
    assert models.valid_read_only_report_repository_executor_attestation(attestation)
    assert models.report_repository_executor_attestation_is_host_install(attestation)


def test_host_install_attestation_may_not_claim_a_container_runtime():
    """The macos_host posture is an assertion of *absence*. A darwin node that
    also claims an image digest, policy or OpenShell binary is rejected: a
    posture string other code trusts must not be able to overstate itself."""

    base = models.read_only_report_repository_executor_attestation(
        runtime_image_ref="",
        policy_sha256="",
        openshell_bin_path="",
        openshell_bin_sha256="",
        executor_path="/opt/mac/bin/mac-task-executor",
        executor_sha256="sha256:" + "a" * 64,
        platform="darwin",
        isolation_posture="macos_host",
        python_path="/usr/bin/python3",
        python_sha256="sha256:" + "b" * 64,
        executor_script_path="/opt/mac/bin/mac-task-executor.py",
        executor_script_sha256="sha256:" + "c" * 64,
        source_root="/opt/mac/src",
        source_bundle_sha256="sha256:" + "d" * 64,
    )
    assert models.valid_read_only_report_repository_executor_attestation(base)
    for key, value in (
        ("runtime_image_ref", _RUNTIME_REF),
        ("policy_sha256", "sha256:" + "e" * 64),
        ("openshell_bin_path", "/usr/local/bin/openshell"),
        ("openshell_bin_sha256", "sha256:" + "f" * 64),
    ):
        overstated = dict(base)
        overstated[key] = value
        assert not models.valid_read_only_report_repository_executor_attestation(
            overstated
        ), key
    # Linux may not borrow the host-install posture, and the retired macOS
    # Docker posture is no longer accepted anywhere.
    for platform, posture in (
        ("linux", "macos_host"),
        ("darwin", "macos_docker_vm_seccomp_egress"),
        ("darwin", "landlock_enforced"),
    ):
        moved = dict(base)
        moved["platform"] = platform
        moved["isolation_posture"] = posture
        assert not models.valid_read_only_report_repository_executor_attestation(moved)


def test_linux_still_requires_a_full_container_bound_attestation():
    """Linux is unchanged by the macOS host-install decision: it must carry
    the managed image digest, the policy digest and the OpenShell binary
    digest, under landlock_enforced."""

    linux = models.read_only_report_repository_executor_attestation(
        runtime_image_ref=_RUNTIME_REF,
        policy_sha256="sha256:" + "e" * 64,
        openshell_bin_path="/usr/local/bin/openshell",
        openshell_bin_sha256="sha256:" + "f" * 64,
        executor_path="/opt/mac/bin/mac-task-executor",
        executor_sha256="sha256:" + "a" * 64,
        platform="linux",
        isolation_posture="landlock_enforced",
        python_path="/opt/mac-venv/bin/python",
        python_sha256="sha256:" + "b" * 64,
        executor_script_path="/opt/mac/bin/mac-task-executor.py",
        executor_script_sha256="sha256:" + "c" * 64,
        source_root="/opt/mac/src",
        source_bundle_sha256="sha256:" + "d" * 64,
    )
    assert models.valid_read_only_report_repository_executor_attestation(linux)
    assert not models.report_repository_executor_attestation_is_host_install(linux)
    for key in (
        "runtime_image_ref",
        "policy_sha256",
        "openshell_bin_path",
        "openshell_bin_sha256",
    ):
        emptied = dict(linux)
        emptied[key] = ""
        assert not models.valid_read_only_report_repository_executor_attestation(
            emptied
        ), key


def _marker_resources(attestation):
    fields = {
        key: attestation[key]
        for key in (
            "runtime_image_ref",
            "policy_sha256",
            "openshell_bin_path",
            "openshell_bin_sha256",
            "executor_path",
            "executor_sha256",
            "platform",
            "isolation_posture",
            "python_path",
            "python_sha256",
            "executor_script_path",
            "executor_script_sha256",
            "source_root",
            "source_bundle_sha256",
        )
    }
    return {
        REPORT_REPOSITORY_EXECUTOR_RESOURCE_KEY: (
            read_only_report_repository_executor_resource(**fields)
        )
    }


def test_policy_drift_after_approval_fails_before_sandbox_create(
    report_boundary_env,
):
    executor, policy = report_boundary_env
    attestation = worker._read_only_report_executor_attestation([str(executor)])
    assert attestation is not None
    assert worker._apply_read_only_report_executor_approval(
        _marker_resources(attestation), os.environ
    )
    policy.write_text("version: 2\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="policy differs"):
        sandbox._read_only_report_extra_create_argv()


def test_host_executor_drift_after_approval_fails_before_popen(
    report_boundary_env,
):
    executor, _policy = report_boundary_env
    attestation = worker._read_only_report_executor_attestation([str(executor)])
    assert attestation is not None
    assert worker._apply_read_only_report_executor_approval(
        _marker_resources(attestation), os.environ
    )
    executor.write_text("#!/bin/sh\nexit 19\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="differ from hub approval"):
        worker_subprocess._assert_approved_read_only_report_host_executor(
            [str(executor)], dict(os.environ)
        )


def test_deployment_realistic_marker_stamps_missing_runtime_paths(
    report_boundary_env, monkeypatch
):
    executor, _policy = report_boundary_env
    attestation = worker._read_only_report_executor_attestation([str(executor)])
    assert attestation is not None
    for name in (
        "MAC_TASK_EXECUTOR_PYTHON",
        "MAC_TASK_EXECUTOR_SCRIPT",
        "MAC_SELF_UPDATE_REPO",
    ):
        monkeypatch.delenv(name, raising=False)
    assert worker._apply_read_only_report_executor_approval(
        _marker_resources(attestation), os.environ
    )
    assert os.environ["MAC_TASK_EXECUTOR_PYTHON"] == attestation["python_path"]
    assert os.environ["MAC_TASK_EXECUTOR_SCRIPT"] == attestation[
        "executor_script_path"
    ]
    assert os.environ["MAC_SELF_UPDATE_REPO"] == attestation["source_root"]
    worker_subprocess._assert_approved_read_only_report_host_executor(
        [str(executor)], dict(os.environ)
    )


def test_retargeted_python_symlink_cannot_change_approved_invocation(
    report_boundary_env, tmp_path, monkeypatch
):
    executor, _policy = report_boundary_env
    original_python = Path(os.environ["MAC_TASK_EXECUTOR_PYTHON"])
    alternate_python = tmp_path / "alternate-python"
    alternate_python.write_bytes(original_python.read_bytes() + b"drift")
    alternate_python.chmod(0o755)
    launcher = tmp_path / "python-launcher"
    launcher.symlink_to(original_python)
    monkeypatch.setenv("MAC_TASK_EXECUTOR_PYTHON", str(launcher))
    attestation = worker._read_only_report_executor_attestation([str(executor)])
    assert attestation is not None
    assert worker._apply_read_only_report_executor_approval(
        _marker_resources(attestation), os.environ
    )
    # Approval stamps the resolved no-follow file, so retargeting the original
    # launcher cannot influence what the generated wrapper will execute.
    launcher.unlink()
    launcher.symlink_to(alternate_python)
    assert os.environ["MAC_TASK_EXECUTOR_PYTHON"] == str(original_python)
    worker_subprocess._assert_approved_read_only_report_host_executor(
        [str(executor)], dict(os.environ)
    )


def test_deployed_report_wrapper_execs_only_approved_absolute_artifacts():
    installer = (
        Path(__file__).resolve().parents[1] / "deploy" / "fleet-node-install.sh"
    ).read_text(encoding="utf-8")
    block = installer.split("cat > \"$executor\" <<'EOF'", 1)[1].split(
        "\nEOF", 1
    )[0]
    assert block.startswith("\n#!/bin/bash")
    assert '. "$HOME/.mac/mac.env"' not in block
    assert 'exec "$MAC_TASK_EXECUTOR_PYTHON" "$MAC_TASK_EXECUTOR_SCRIPT"' in block
    assert 'exec "$HOME/.mac/venv/bin/python"' not in block
    assert 'install -m 0755 "$resolved_python" "${report_python}.new"' in installer


def test_model_sandbox_never_receives_fleet_hub_credentials(
    report_boundary_env, monkeypatch
):
    monkeypatch.delenv("MAC_TASK_REPO_ACCESS_MODE", raising=False)
    monkeypatch.delenv("MAC_TASK_REPO_ACCESS_SCHEMA", raising=False)
    for name in (
        "MAC_WORKER_TOKEN",
        "MAC_TOKEN",
        "MAC_API_TOKEN",
        "MAC_ATTESTATION_KEY",
        "MAC_HUB_TOKEN",
    ):
        monkeypatch.setenv(name, "secret-value")
    environment = sandbox._openshell_environment()
    assert not set(environment).intersection(
        {
            "MAC_WORKER_TOKEN",
            "MAC_TOKEN",
            "MAC_API_TOKEN",
            "MAC_ATTESTATION_KEY",
            "MAC_HUB_TOKEN",
        }
    )


def test_custom_attestation_key_passthrough_removes_report_attestation(
    report_boundary_env, monkeypatch
):
    executor, _policy = report_boundary_env
    monkeypatch.setenv("MAC_OPENSHELL_ENV_PASSTHROUGH", "MAC_ATTESTATION_KEY")
    monkeypatch.setenv("MAC_ATTESTATION_KEY", "secret-value")
    monkeypatch.setenv("MAC_TASK_REPO_ACCESS_MODE", "read_only")
    monkeypatch.setenv(
        "MAC_TASK_REPO_ACCESS_SCHEMA", "mac.report_repository_access.v1"
    )
    assert worker._read_only_report_executor_attestation([str(executor)]) is None
    with pytest.raises(ValueError, match="non-allowlisted"):
        sandbox._openshell_environment()


def test_drift_heartbeat_removes_marker_before_next_claim(monkeypatch):
    cp = ControlPlane.in_memory()
    admitted = _agent(cp, "heartbeat-drift", ["ops"], attested=True)

    class Client:
        def get(self, path):
            assert path.endswith(admitted.id)
            return cp.get_agent(admitted.id).to_dict()

        def post(self, path, body):
            assert path.endswith("/heartbeat")
            return cp.heartbeat_agent(admitted.id, **body).to_dict()

    mac_worker = worker.MacWorker(
        Client(),
        admitted.id,
        Path("/tmp/mac-heartbeat-drift"),
        worker.SubprocessExecutor(["/approved/mac-task-executor"]),
    )
    monkeypatch.setattr(mac_worker, "_maybe_start_coding_route_probe", lambda: None)
    monkeypatch.setattr(mac_worker, "_maybe_command_inventory_resources", lambda: None)
    monkeypatch.setattr(worker, "_read_only_report_executor_attestation", lambda _argv: None)
    mac_worker._heartbeat()
    refreshed = cp.get_agent(admitted.id)
    assert not agent_has_read_only_report_repository_executor(refreshed.resources)
    with pytest.raises(ValidationError, match="report_repository_executor_missing"):
        cp.claim_task(_report_task(cp).id, admitted.id)


@pytest.mark.parametrize(
    "create_args",
    [
        "--from image --policy /tmp/evil.yaml",
        "--from image --name shared-sandbox",
        "--from image --upload /host:/sandbox/host",
        "--from image --driver host",
        "--from first --from second",
    ],
)
def test_report_boundary_rejects_policy_name_upload_and_driver_overrides(
    report_boundary_env,
    monkeypatch,
    create_args,
):
    executor, _policy = report_boundary_env
    monkeypatch.setenv("MAC_OPENSHELL_CREATE_ARGS", create_args)
    with pytest.raises(ValueError, match="forbid|duplicate"):
        sandbox._read_only_report_extra_create_argv(require_approval=False)
    assert worker._read_only_report_executor_attestation([str(executor)]) is None


def test_report_boundary_rejects_fixed_sandbox_name(
    report_boundary_env,
    monkeypatch,
):
    executor, _policy = report_boundary_env
    monkeypatch.setenv("MAC_OPENSHELL_SANDBOX_NAME", "shared")
    with pytest.raises(RuntimeError, match="fresh per-task sandbox"):
        sandbox._read_only_report_extra_create_argv(require_approval=False)
    assert worker._read_only_report_executor_attestation([str(executor)]) is None
