"""Contract coverage for the typed static-host OpenShell runtime refresh.

The four scenarios the refresh path exists to make provable:

* an unchanged runtime re-proves its attestation without mutating anything,
* a requested runtime upgrade installs the reviewed multi-arch digest and ends
  on an attestation that equals it,
* a failure *before* the mutation boundary leaves the host untouched, and
* a failure *after* it emits fix-forward recovery evidence instead of a
  rollback to the superseded digest.
"""

from __future__ import annotations

import json

import pytest

from mac.models import read_only_report_repository_executor_attestation
from mac.openshell_static_runtime_refresh import (
    INVENTORY_PROOF_SCHEMA,
    OUTCOME_FAILED_AFTER_MUTATION,
    OUTCOME_FAILED_BEFORE_MUTATION,
    OUTCOME_UNCHANGED,
    OUTCOME_UPGRADED,
    PHASE_ATTEST,
    PHASE_INSTALL,
    PHASE_INVENTORY,
    PHASE_PRESERVE,
    PHASE_REPLACE,
    PHASE_RESTORE,
    RECOVERY_SCHEMA,
    REFRESH_PLAN_SCHEMA,
    REFRESH_RECEIPT_SCHEMA,
    REFRESH_REQUEST_SCHEMA,
    SandboxRecord,
    StaticRuntimeRefreshError,
    StaticRuntimeRefreshRequest,
    attestation_matches_requested_runtime,
    build_inventory_proof,
    execute_static_runtime_refresh,
    main,
    plan_static_runtime_refresh,
    request_from_json,
    runtime_digest,
    sandbox_record_from_json,
    validate_install_receipt,
)


RUNTIME = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "5b" * 32
OLD_RUNTIME = "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "c7" * 32
FROZEN_INPUTS = "sha256:" + "79" * 32
AMD64_DIGEST = "sha256:" + "a1" * 32
ARM64_DIGEST = "sha256:" + "b2" * 32


def _request(**overrides) -> StaticRuntimeRefreshRequest:
    values = {
        "host": "static-host-1",
        "agent_id": "agent_worker1",
        "requested_runtime_image_ref": RUNTIME,
        "frozen_inputs_sha256": FROZEN_INPUTS,
        "source_commit": "1cf74c494f38d5082ba4a59dc8b8e2e8a14d996c",
        "review_id": "review_runtime_identity_1",
    }
    values.update(overrides)
    return StaticRuntimeRefreshRequest(**values)


def _attestation(runtime_image_ref: str = RUNTIME) -> dict:
    digest = "sha256:" + "0f" * 32
    return read_only_report_repository_executor_attestation(
        runtime_image_ref=runtime_image_ref,
        policy_sha256=digest,
        openshell_bin_path="/usr/local/bin/openshell",
        openshell_bin_sha256=digest,
        executor_path="/opt/mac/executor",
        executor_sha256=digest,
        platform="linux",
        isolation_posture="landlock_enforced",
        python_path="/opt/mac-venv/bin/python",
        python_sha256=digest,
        executor_script_path="/opt/mac/executor.py",
        executor_script_sha256=digest,
        source_root="/opt/mac/src",
        source_bundle_sha256=digest,
    )


def _install_receipt(image_ref: str = RUNTIME) -> dict:
    return {
        "image_ref": image_ref,
        "manifest_list_digest": runtime_digest(image_ref),
        "platform_digests": {
            "linux/amd64": AMD64_DIGEST,
            "linux/arm64": ARM64_DIGEST,
        },
    }


class FakeEffects:
    """A scriptable stand-in for the static host's OpenShell surfaces."""

    def __init__(
        self,
        *,
        installed_ref: str = OLD_RUNTIME,
        inventory=None,
        post_inventory=None,
        attestation=None,
        install_receipt=None,
        install_error: Exception | None = None,
        inventory_error: Exception | None = None,
        preserve_receipt=None,
        replacement=None,
        restoration=None,
        recovery_installed_ref: str | None = None,
    ):
        self.installed_ref = installed_ref
        self._inventory = list(inventory or [])
        self._post_inventory = post_inventory
        self._attestation = attestation if attestation is not None else _attestation()
        self._install_receipt = install_receipt
        self._install_error = install_error
        self._inventory_error = inventory_error
        self._preserve_receipt = preserve_receipt
        self._replacement = replacement
        self._restoration = restoration
        self._recovery_installed_ref = recovery_installed_ref
        self.calls: list[str] = []
        self.replaced_with: list[tuple[list[str], str]] = []
        self._inventory_reads = 0

    def installed_runtime_image_ref(self) -> str:
        self.calls.append("installed_runtime_image_ref")
        if self._recovery_installed_ref is not None and "install_runtime_image" in self.calls:
            return self._recovery_installed_ref
        return self.installed_ref

    def sandbox_inventory(self):
        self.calls.append("sandbox_inventory")
        if self._inventory_error is not None:
            raise self._inventory_error
        self._inventory_reads += 1
        if self._inventory_reads > 1 and self._post_inventory is not None:
            return list(self._post_inventory)
        return list(self._inventory)

    def preserve_openclaw_state(self):
        self.calls.append("preserve_openclaw_state")
        if self._preserve_receipt is not None:
            return self._preserve_receipt
        return {
            "session_ids": sorted(
                {
                    record.openclaw_session_id
                    for record in self._inventory
                    if record.openclaw_session_id
                }
            ),
            "checkpoint_sha256": "sha256:" + "cc" * 32,
        }

    def install_runtime_image(self, image_ref: str):
        self.calls.append("install_runtime_image")
        if self._install_error is not None:
            raise self._install_error
        self.installed_ref = image_ref
        if self._install_receipt is not None:
            return self._install_receipt
        return _install_receipt(image_ref)

    def replace_sandboxes(self, sandbox_ids, image_ref: str):
        self.calls.append("replace_sandboxes")
        self.replaced_with.append((list(sandbox_ids), image_ref))
        if self._replacement is not None:
            return self._replacement
        return {"replaced_sandbox_ids": list(sandbox_ids), "failed_sandbox_ids": []}

    def restore_openclaw_state(self, preservation):
        self.calls.append("restore_openclaw_state")
        if self._restoration is not None:
            return self._restoration
        return {"restored_session_ids": list(preservation.get("session_ids") or [])}

    def report_executor_attestation(self):
        self.calls.append("report_executor_attestation")
        return self._attestation


def _sandbox(sandbox_id: str, ref: str, *, state: str = "ready", **kwargs) -> SandboxRecord:
    return SandboxRecord(
        sandbox_id=sandbox_id, state=state, runtime_image_ref=ref, **kwargs
    )


# --------------------------------------------------------------------------
# Request / plan shape
# --------------------------------------------------------------------------


def test_runtime_digest_extracts_the_pinned_sha256():
    assert runtime_digest(RUNTIME) == "sha256:" + "5b" * 32


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"host": " "}, "host is required"),
        ({"agent_id": ""}, "agent id is required"),
        ({"requested_runtime_image_ref": "docker.io/other@sha256:" + "ab" * 32}, "managed immutable"),
        ({"frozen_inputs_sha256": "deadbeef"}, "sha256"),
        ({"source_commit": ""}, "source commit is required"),
        # An unreviewed mutation is exactly what the static phase forbids.
        ({"review_id": ""}, "review id is required"),
        ({"platforms": ()}, "at least one platform"),
        ({"platforms": ("",)}, "platform is required"),
    ],
)
def test_request_validation_rejects_incomplete_requests(overrides, message):
    with pytest.raises(StaticRuntimeRefreshError) as excinfo:
        _request(**overrides).validate()
    assert message in str(excinfo.value)


def test_request_round_trips_through_json():
    request = _request()
    payload = request.as_json()
    assert payload["schema"] == REFRESH_REQUEST_SCHEMA
    assert request_from_json(payload) == request


def test_request_from_json_rejects_bad_envelopes():
    with pytest.raises(StaticRuntimeRefreshError):
        request_from_json(["not", "a", "mapping"])
    with pytest.raises(StaticRuntimeRefreshError):
        request_from_json({"schema": "mac.other.v1"})
    payload = _request().as_json()
    payload["platforms"] = "linux/amd64"
    with pytest.raises(StaticRuntimeRefreshError):
        request_from_json(payload)


def test_request_from_json_defaults_to_multi_architecture_platforms():
    payload = _request().as_json()
    payload.pop("platforms")
    assert request_from_json(payload).platforms == ("linux/amd64", "linux/arm64")


def test_sandbox_record_json_round_trip_and_busy_detection():
    record = _sandbox("sbx-1", RUNTIME, state="running", openclaw_session_id="s1")
    assert record.busy is True
    assert sandbox_record_from_json(record.as_json()) == record
    assert _sandbox("sbx-2", RUNTIME, task_id="task_1").busy is True
    assert _sandbox("sbx-3", RUNTIME).busy is False
    with pytest.raises(StaticRuntimeRefreshError):
        sandbox_record_from_json("nope")


def test_plan_requires_no_mutation_when_host_already_matches():
    plan = plan_static_runtime_refresh(
        _request(),
        installed_runtime_image_ref=RUNTIME,
        inventory=[_sandbox("sbx-1", RUNTIME)],
    )
    assert plan["schema"] == REFRESH_PLAN_SCHEMA
    assert plan["mutation_required"] is False
    assert plan["phases"] == [PHASE_INVENTORY, PHASE_ATTEST]
    assert plan["mutating_phases"] == []


def test_plan_for_requested_upgrade_covers_every_phase():
    plan = plan_static_runtime_refresh(
        _request(),
        installed_runtime_image_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
    )
    assert plan["runtime_change_required"] is True
    assert plan["stale_sandbox_ids"] == ["sbx-1"]
    assert plan["phases"] == [
        PHASE_INVENTORY,
        PHASE_PRESERVE,
        PHASE_INSTALL,
        PHASE_REPLACE,
        PHASE_RESTORE,
        PHASE_ATTEST,
    ]
    assert plan["mutating_phases"] == [PHASE_INSTALL, PHASE_REPLACE, PHASE_RESTORE]


def test_plan_skips_install_when_only_sandboxes_drifted():
    plan = plan_static_runtime_refresh(
        _request(),
        installed_runtime_image_ref=RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
    )
    assert plan["runtime_change_required"] is False
    assert plan["mutation_required"] is True
    assert PHASE_INSTALL not in plan["phases"]


def test_plan_rejects_an_unmanaged_installed_reference():
    with pytest.raises(StaticRuntimeRefreshError):
        plan_static_runtime_refresh(
            _request(), installed_runtime_image_ref="", inventory=[]
        )


# --------------------------------------------------------------------------
# Inventory / quiescence proof
# --------------------------------------------------------------------------


def test_inventory_proof_reports_stale_and_session_identity():
    proof = build_inventory_proof(
        [
            _sandbox("sbx-1", RUNTIME, openclaw_session_id="s1"),
            _sandbox("sbx-2", OLD_RUNTIME),
        ],
        requested_runtime_image_ref=RUNTIME,
        installed_runtime_image_ref=OLD_RUNTIME,
        require_quiescent=True,
    )
    assert proof["schema"] == INVENTORY_PROOF_SCHEMA
    assert proof["quiescent"] is True
    assert proof["stale_sandbox_ids"] == ["sbx-2"]
    assert proof["openclaw_session_ids"] == ["s1"]
    assert proof["sandbox_count"] == 2


def test_inventory_proof_blocks_a_busy_host_when_quiescence_is_required():
    records = [_sandbox("sbx-1", OLD_RUNTIME, state="running")]
    with pytest.raises(StaticRuntimeRefreshError) as excinfo:
        build_inventory_proof(
            records,
            requested_runtime_image_ref=RUNTIME,
            installed_runtime_image_ref=OLD_RUNTIME,
            require_quiescent=True,
        )
    assert "not quiescent" in str(excinfo.value)
    tolerated = build_inventory_proof(
        records,
        requested_runtime_image_ref=RUNTIME,
        installed_runtime_image_ref=OLD_RUNTIME,
        require_quiescent=False,
    )
    assert tolerated["quiescent"] is False


# --------------------------------------------------------------------------
# Install receipt / attestation gates
# --------------------------------------------------------------------------


def test_install_receipt_requires_every_requested_architecture():
    receipt = _install_receipt()
    assert validate_install_receipt(
        receipt,
        requested_runtime_image_ref=RUNTIME,
        platforms=("linux/amd64", "linux/arm64"),
    )["platform_digests"] == {
        "linux/amd64": AMD64_DIGEST,
        "linux/arm64": ARM64_DIGEST,
    }
    single_arch = _install_receipt()
    single_arch["platform_digests"].pop("linux/arm64")
    with pytest.raises(StaticRuntimeRefreshError) as excinfo:
        validate_install_receipt(
            single_arch,
            requested_runtime_image_ref=RUNTIME,
            platforms=("linux/amd64", "linux/arm64"),
        )
    assert "multi-architecture" in str(excinfo.value)


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda r: r.update(image_ref=OLD_RUNTIME), "different runtime image reference"),
        (lambda r: r.update(platform_digests=["linux/amd64"]), "per-platform digests"),
        (lambda r: r["platform_digests"].update({"linux/amd64": "nope"}), "sha256"),
        (
            lambda r: r.update(manifest_list_digest="sha256:" + "ee" * 32),
            "manifest digest differs",
        ),
    ],
)
def test_install_receipt_rejects_malformed_receipts(mutate, message):
    receipt = _install_receipt()
    mutate(receipt)
    with pytest.raises(StaticRuntimeRefreshError) as excinfo:
        validate_install_receipt(
            receipt,
            requested_runtime_image_ref=RUNTIME,
            platforms=("linux/amd64", "linux/arm64"),
        )
    assert message in str(excinfo.value)
    with pytest.raises(StaticRuntimeRefreshError):
        validate_install_receipt(
            "not-a-mapping",
            requested_runtime_image_ref=RUNTIME,
            platforms=("linux/amd64",),
        )


def test_attestation_gate_requires_validity_and_the_requested_digest():
    assert attestation_matches_requested_runtime(_attestation(), RUNTIME) is True
    # The exact regression: a complete, valid attestation that still names the
    # superseded runtime digest is a failure, not a pass.
    assert attestation_matches_requested_runtime(_attestation(OLD_RUNTIME), RUNTIME) is False
    incomplete = _attestation()
    incomplete.pop("policy_sha256")
    assert attestation_matches_requested_runtime(incomplete, RUNTIME) is False


# --------------------------------------------------------------------------
# Scenario 1 — unchanged runtime
# --------------------------------------------------------------------------


def test_unchanged_runtime_reproves_attestation_without_mutating():
    effects = FakeEffects(
        installed_ref=RUNTIME,
        inventory=[_sandbox("sbx-1", RUNTIME, openclaw_session_id="s1")],
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["schema"] == REFRESH_RECEIPT_SCHEMA
    assert receipt["outcome"] == OUTCOME_UNCHANGED
    assert receipt["ok"] is True
    assert receipt["mutated"] is False
    assert receipt["phases_completed"] == [PHASE_INVENTORY, PHASE_ATTEST]
    assert receipt["attestation_matches_request"] is True
    assert "install_runtime_image" not in effects.calls
    assert "replace_sandboxes" not in effects.calls
    assert "recovery" not in receipt


def test_unchanged_runtime_still_fails_on_a_stale_attestation():
    effects = FakeEffects(
        installed_ref=RUNTIME,
        inventory=[_sandbox("sbx-1", RUNTIME)],
        attestation=_attestation(OLD_RUNTIME),
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_BEFORE_MUTATION
    assert receipt["failed_phase"] == PHASE_ATTEST
    assert receipt["mutated"] is False


def test_unchanged_runtime_does_not_require_a_quiescent_host():
    effects = FakeEffects(
        installed_ref=RUNTIME,
        inventory=[_sandbox("sbx-1", RUNTIME, state="running", task_id="task_1")],
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_UNCHANGED
    assert receipt["inventory_proof"]["quiescent"] is False


# --------------------------------------------------------------------------
# Scenario 2 — requested runtime upgrade
# --------------------------------------------------------------------------


def test_requested_upgrade_installs_the_reviewed_digest_and_reattests():
    effects = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME, openclaw_session_id="s1")],
        post_inventory=[_sandbox("sbx-1", RUNTIME, openclaw_session_id="s1")],
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_UPGRADED
    assert receipt["ok"] is True
    assert receipt["mutated"] is True
    assert receipt["phases_completed"] == [
        PHASE_INVENTORY,
        PHASE_PRESERVE,
        PHASE_INSTALL,
        PHASE_REPLACE,
        PHASE_RESTORE,
        PHASE_ATTEST,
    ]
    # OpenClaw state is captured before the first mutation and restored after.
    assert effects.calls.index("preserve_openclaw_state") < effects.calls.index(
        "install_runtime_image"
    )
    assert receipt["openclaw_preservation"]["session_ids"] == ["s1"]
    assert receipt["openclaw_restoration"] == {
        "restored_session_ids": ["s1"],
        "restored": True,
    }
    assert receipt["install_receipt"]["platform_digests"] == {
        "linux/amd64": AMD64_DIGEST,
        "linux/arm64": ARM64_DIGEST,
    }
    assert effects.replaced_with == [(["sbx-1"], RUNTIME)]
    assert receipt["sandbox_replacement"]["post_inventory_proof"]["stale_sandbox_ids"] == []
    assert receipt["attestation_matches_request"] is True
    assert receipt["requested_runtime_digest"] == runtime_digest(RUNTIME)


def test_upgrade_without_install_when_only_sandboxes_drifted():
    effects = FakeEffects(
        installed_ref=RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
        post_inventory=[_sandbox("sbx-1", RUNTIME)],
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_UPGRADED
    assert "install_runtime_image" not in effects.calls
    assert PHASE_INSTALL not in receipt["phases_completed"]


# --------------------------------------------------------------------------
# Scenario 3 — failure before the mutation boundary
# --------------------------------------------------------------------------


def test_busy_sandbox_stops_the_refresh_before_any_mutation():
    effects = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME, state="running", task_id="task_1")],
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_BEFORE_MUTATION
    assert receipt["ok"] is False
    assert receipt["mutated"] is False
    assert receipt["failed_phase"] == PHASE_INVENTORY
    assert "not quiescent" in receipt["error"]
    assert effects.calls == ["installed_runtime_image_ref", "sandbox_inventory"]
    assert "recovery" not in receipt


def test_install_failure_before_the_pin_moves_is_pre_mutation():
    effects = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
        install_error=RuntimeError("registry pull failed"),
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_BEFORE_MUTATION
    assert receipt["failed_phase"] == PHASE_INSTALL
    assert receipt["mutated"] is False
    assert receipt["error"] == "registry pull failed"


def test_unpreservable_openclaw_state_stops_before_mutation():
    effects = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
        preserve_receipt={"session_ids": ["s1"]},
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_BEFORE_MUTATION
    assert receipt["failed_phase"] == PHASE_PRESERVE
    assert "install_runtime_image" not in effects.calls

    malformed = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
        preserve_receipt={"session_ids": "s1", "checkpoint_sha256": "sha256:" + "cc" * 32},
    )
    assert (
        execute_static_runtime_refresh(_request(), malformed)["failed_phase"]
        == PHASE_PRESERVE
    )
    assert (
        execute_static_runtime_refresh(
            _request(),
            FakeEffects(
                installed_ref=OLD_RUNTIME,
                inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
                preserve_receipt="nope",
            ),
        )["failed_phase"]
        == PHASE_PRESERVE
    )


def test_unreadable_host_state_fails_before_a_plan_exists():
    effects = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[],
        inventory_error=RuntimeError("supervisor unreachable"),
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_BEFORE_MUTATION
    assert receipt["plan"]["phases"] == []
    assert receipt["error"] == "supervisor unreachable"


def test_execute_rejects_a_malformed_request_outright():
    with pytest.raises(StaticRuntimeRefreshError):
        execute_static_runtime_refresh(_request(review_id=""), FakeEffects())


# --------------------------------------------------------------------------
# Scenario 4 — failure after mutation, fix-forward recovery
# --------------------------------------------------------------------------


def test_stale_attestation_after_install_yields_fix_forward_recovery():
    effects = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME, openclaw_session_id="s1")],
        post_inventory=[_sandbox("sbx-1", RUNTIME, openclaw_session_id="s1")],
        attestation=_attestation(OLD_RUNTIME),
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_AFTER_MUTATION
    assert receipt["ok"] is False
    assert receipt["mutated"] is True
    assert receipt["failed_phase"] == PHASE_ATTEST
    recovery = receipt["recovery"]
    assert recovery["schema"] == RECOVERY_SCHEMA
    assert recovery["strategy"] == "fix_forward"
    assert recovery["rollback_performed"] is False
    assert recovery["rollback_forbidden_reason"]
    assert recovery["observed_runtime_image_ref"] == RUNTIME
    assert recovery["residual_stale_sandbox_ids"] == []
    assert recovery["observed_attestation_runtime_image_ref"] == OLD_RUNTIME
    assert recovery["attestation_matches_request"] is False
    assert recovery["converged"] is False
    assert recovery["next_action"] == "retry_static_runtime_refresh"
    assert recovery["openclaw_state_preserved"] is True
    assert recovery["openclaw_restore_attempted"] is True


def test_residual_stale_sandbox_after_replacement_is_post_mutation():
    effects = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME), _sandbox("sbx-2", OLD_RUNTIME)],
        post_inventory=[_sandbox("sbx-1", RUNTIME), _sandbox("sbx-2", OLD_RUNTIME)],
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_AFTER_MUTATION
    assert receipt["failed_phase"] == PHASE_REPLACE
    assert "sbx-2" in receipt["error"]
    assert receipt["recovery"]["residual_stale_sandbox_ids"] == ["sbx-2"]


def test_replacement_receipt_failures_are_post_mutation():
    for replacement in (
        "not-a-mapping",
        {"replaced_sandbox_ids": "sbx-1"},
        {"replaced_sandbox_ids": ["sbx-1"], "failed_sandbox_ids": "sbx-1"},
        {"replaced_sandbox_ids": ["sbx-1"], "failed_sandbox_ids": ["sbx-1"]},
        {"replaced_sandbox_ids": [], "failed_sandbox_ids": []},
    ):
        effects = FakeEffects(
            installed_ref=OLD_RUNTIME,
            inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
            post_inventory=[_sandbox("sbx-1", RUNTIME)],
            replacement=replacement,
        )
        receipt = execute_static_runtime_refresh(_request(), effects)
        assert receipt["outcome"] == OUTCOME_FAILED_AFTER_MUTATION
        assert receipt["failed_phase"] == PHASE_REPLACE


def test_incomplete_openclaw_restoration_is_post_mutation():
    for restoration in (
        "not-a-mapping",
        {"restored_session_ids": "s1"},
        {"restored_session_ids": []},
    ):
        effects = FakeEffects(
            installed_ref=OLD_RUNTIME,
            inventory=[_sandbox("sbx-1", OLD_RUNTIME, openclaw_session_id="s1")],
            post_inventory=[_sandbox("sbx-1", RUNTIME, openclaw_session_id="s1")],
            restoration=restoration,
        )
        receipt = execute_static_runtime_refresh(_request(), effects)
        assert receipt["outcome"] == OUTCOME_FAILED_AFTER_MUTATION
        assert receipt["failed_phase"] == PHASE_RESTORE


def test_invalid_install_receipt_is_post_mutation_because_the_pin_moved():
    effects = FakeEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME)],
        post_inventory=[_sandbox("sbx-1", RUNTIME)],
        install_receipt={"image_ref": RUNTIME, "platform_digests": {}},
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_AFTER_MUTATION
    assert receipt["failed_phase"] == PHASE_INSTALL
    assert receipt["recovery"]["openclaw_restore_attempted"] is True


def test_recovery_evidence_survives_unreadable_host_surfaces():
    class BrokenEffects(FakeEffects):
        def installed_runtime_image_ref(self):
            self.calls.append("installed_runtime_image_ref")
            if "install_runtime_image" in self.calls:
                raise RuntimeError("pin unreadable")
            return self.installed_ref

        def sandbox_inventory(self):
            self.calls.append("sandbox_inventory")
            if self.calls.count("sandbox_inventory") > 2:
                raise RuntimeError("supervisor unreachable")
            if self.calls.count("sandbox_inventory") == 2:
                return [_sandbox("sbx-1", RUNTIME)]
            return list(self._inventory)

        def restore_openclaw_state(self, preservation):
            self.calls.append("restore_openclaw_state")
            raise RuntimeError("checkpoint replay failed")

    effects = BrokenEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME, openclaw_session_id="s1")],
        attestation=_attestation(OLD_RUNTIME),
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_AFTER_MUTATION
    recovery = receipt["recovery"]
    assert recovery["runtime_pin_readable"] is False
    assert recovery["sandbox_inventory_readable"] is False
    assert recovery["observed_runtime_image_ref"] == ""
    assert recovery["openclaw_restore_ok"] is False
    assert recovery["converged"] is False


def test_recovery_reports_convergence_when_the_retry_surface_is_already_correct():
    class LateConvergingEffects(FakeEffects):
        def restore_openclaw_state(self, preservation):
            self.calls.append("restore_openclaw_state")
            if self.calls.count("restore_openclaw_state") == 1:
                raise RuntimeError("transient checkpoint replay failure")
            return {"restored_session_ids": list(preservation.get("session_ids") or [])}

    effects = LateConvergingEffects(
        installed_ref=OLD_RUNTIME,
        inventory=[_sandbox("sbx-1", OLD_RUNTIME, openclaw_session_id="s1")],
        post_inventory=[_sandbox("sbx-1", RUNTIME, openclaw_session_id="s1")],
    )
    receipt = execute_static_runtime_refresh(_request(), effects)
    assert receipt["outcome"] == OUTCOME_FAILED_AFTER_MUTATION
    assert receipt["failed_phase"] == PHASE_RESTORE
    recovery = receipt["recovery"]
    assert recovery["converged"] is True
    assert recovery["openclaw_restore_ok"] is True
    assert recovery["next_action"] == PHASE_ATTEST


# --------------------------------------------------------------------------
# CLI surface
# --------------------------------------------------------------------------


def _write(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def test_cli_plan_emits_the_typed_plan(tmp_path, capsys):
    request_path = _write(tmp_path, "request.json", _request().as_json())
    inventory_path = _write(
        tmp_path, "inventory.json", [_sandbox("sbx-1", OLD_RUNTIME).as_json()]
    )
    code = main(
        [
            "admin", "plan",
            "--request",
            request_path,
            "--installed-ref",
            OLD_RUNTIME,
            "--inventory",
            inventory_path,
        ]
    )
    assert code == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["schema"] == REFRESH_PLAN_SCHEMA
    assert plan["stale_sandbox_ids"] == ["sbx-1"]


def test_cli_plan_rejects_a_non_list_inventory(tmp_path, capsys):
    request_path = _write(tmp_path, "request.json", _request().as_json())
    inventory_path = _write(tmp_path, "inventory.json", {"sbx-1": {}})
    code = main(
        [
            "admin", "plan",
            "--request",
            request_path,
            "--installed-ref",
            OLD_RUNTIME,
            "--inventory",
            inventory_path,
        ]
    )
    assert code == 2
    assert "must be a list" in capsys.readouterr().err


def test_cli_plan_reports_unreadable_inputs(tmp_path, capsys):
    code = main(
        [
            "admin", "plan",
            "--request",
            str(tmp_path / "missing.json"),
            "--installed-ref",
            OLD_RUNTIME,
            "--inventory",
            str(tmp_path / "missing.json"),
        ]
    )
    assert code == 2
    assert "unreadable" in capsys.readouterr().err


def test_cli_verify_attestation_exit_codes(tmp_path, capsys):
    matching = _write(tmp_path, "match.json", _attestation())
    assert main(["verify-attestation", "--attestation", matching, "--requested-ref", RUNTIME]) == 0
    assert json.loads(capsys.readouterr().out)["attestation_matches_request"] is True
    stale = _write(tmp_path, "stale.json", _attestation(OLD_RUNTIME))
    assert main(["verify-attestation", "--attestation", stale, "--requested-ref", RUNTIME]) == 1
    assert json.loads(capsys.readouterr().out)["attestation_matches_request"] is False
    assert (
        main(["verify-attestation", "--attestation", stale, "--requested-ref", "nope"]) == 2
    )
