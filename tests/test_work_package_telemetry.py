from __future__ import annotations

import json

import pytest

from mac.models import ValidationError, json_dumps, utcnow
from mac.services import ControlPlane
from mac.store import StoreError
from mac.test_support import drop_table_guards, ephemeral_dsn, ephemeral_store, store_on
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_service import RepositoryBaseAttestation
from mac.work_package_pipeline import control_plane_pipeline_observer
from mac.work_package_telemetry import deterministic_cohort_assignment


class _Verifier:
    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=planning_base_sha,
            source_kind="test",
            verified_at=utcnow(),
            resource_namespace={"status": "unresolved"},
        )


def _register_repository(cp: ControlPlane) -> None:
    now = utcnow()
    cp.store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "projectrepo_telemetry",
            "telemetry",
            "/tmp/telemetry",
            "git@example.invalid:telemetry.git",
            "telemetry",
            "[]",
            1,
            60,
            "{}",
            now,
            now,
        ),
    )


def _plan() -> dict:
    return {
        "schema": WORK_PACKAGE_PLAN_SCHEMA,
        "package_id": "wp_telemetry",
        "goal": "Measure the managed line",
        "project": "telemetry",
        "repository_id": "projectrepo_telemetry",
        "planning_base_ref": "refs/heads/main",
        "planning_base_sha": "a" * 40,
        "plan_generation": 1,
        "max_in_flight": 1,
        "nodes": [
            {
                "node_key": "build",
                "title": "Build",
                "node_type": "mutation",
                "effects": {"writes": ["src"]},
                "expected_outputs": ["candidate"],
                "verification": {"profile": "repository-default"},
                "estimates": {"confidence": "high"},
            },
            {
                "node_key": "assemble",
                "title": "Assemble",
                "node_type": "integration",
                "depends_on": ["build"],
                "expected_outputs": ["tree"],
                "verification": {"profile": "integration-default"},
            },
            {
                "node_key": "certify",
                "title": "Certify",
                "node_type": "certification",
                "depends_on": ["assemble"],
                "expected_outputs": ["certificate"],
                "verification": {"profile": "certification-default"},
            },
        ],
    }


def _primary_task(
    cp: ControlPlane,
    *,
    task_id: str,
    treatment_route: str,
    state: str = "open",
    completed_at: str | None = None,
) -> None:
    created_at = "2026-07-17T10:00:00.000000Z"
    updated_at = completed_at or created_at
    cp.store.execute(
        "INSERT INTO tasks ("
        "id, title, description, priority, state, required_capabilities, "
        "dependencies, metadata, attempt_count, max_attempts, completed_at, "
        "created_at, updated_at"
        ") VALUES (?, ?, '', 0, ?, '[]', '[]', '{}', 0, 3, ?, ?, ?)",
        (task_id, task_id, state, completed_at, created_at, updated_at),
    )
    cp.work_package_telemetry.assign_cohort(
        task_id=task_id,
        package_id=None,
        eligibility="eligible",
        treatment_route=treatment_route,
        rollout_revision=1,
        cohort_key="test-primary-%s" % treatment_route,
        reason="concurrent_exogenous_assignment",
        actor="execution-cohort-controller",
        assigned_at="2026-07-17T09:00:00.000000Z",
        detail={
            "schema": "mac.execution_cohort.prospective.v3",
            "primary_analysis_eligible": True,
            "estimand": "intention_to_treat_canonical_publication_outcome",
            "randomization": {
                "algorithm": "hmac_sha256_bucket_v1",
                "treatment_route": treatment_route,
            },
        },
    )


def _managed_primary_package(
    cp: ControlPlane,
    *,
    task_id: str,
    package_id: str,
    task_state: str = "open",
    task_completed_at: str | None = None,
) -> str:
    _primary_task(
        cp,
        task_id=task_id,
        treatment_route="managed_synchronized",
        state=task_state,
        completed_at=task_completed_at,
    )
    plan = _plan()
    plan["package_id"] = package_id
    return cp.work_packages.admit(
        plan,
        actor="planner",
        reason="primary endpoint fixture",
        root_task_id=task_id,
    ).package.id


def test_new_legacy_tasks_receive_prospective_immutable_control_assignment() -> None:
    cp = ControlPlane.in_memory()
    try:
        task = cp.create_task("Non-repository control task", actor="operator")
        exported = cp.work_package_telemetry_export(
            treatment_route="legacy_async",
            eligibility="ineligible",
        )
        assignment = next(
            row for row in exported["cohort_assignments"] if row["task_id"] == task.id
        )
        assert assignment["schema"] == "mac.execution_cohort_assignment.v1"
        assert assignment["treatment_route"] == "legacy_async"
        assert assignment["eligibility"] == "ineligible"
        assert assignment["reason"] == "atomic_fast_lane_shape_ineligible"
        assert assignment["detail"]["shape_blockers"]

        with pytest.raises(StoreError, match="immutable"):
            cp.store.execute(
                "UPDATE execution_cohort_assignments SET eligibility = 'eligible' "
                "WHERE id = ?",
                (assignment["id"],),
            )

        events = cp.list_events(subject_type="task", subject_id=task.id, limit=100)
        cohort_event = next(
            event
            for event in events
            if event["event_type"] == "execution.cohort_assigned"
        )
        assert cohort_event["detail"]["treatment_route"] == "legacy_async"
    finally:
        cp.store.close()


def test_managed_assignment_and_pipeline_attempts_are_exported_append_only() -> None:
    cp = ControlPlane.in_memory()
    try:
        _register_repository(cp)
        cp.work_packages.repository_verifier = _Verifier()
        admitted = cp.work_packages.admit(
            _plan(), actor="planner", reason="prospective managed cohort"
        )
        package_id = admitted.package.id
        initial = cp.work_package_telemetry_export(package_id=package_id)
        assert initial["cohort_assignments"][0]["eligibility"] == "ineligible"
        assert (
            initial["cohort_assignments"][0]["treatment_route"]
            == "managed_synchronized"
        )
        assert initial["station_attempts"][0]["station"] == "admission"
        assert initial["station_attempts"][0]["terminal_status"] == "succeeded"

        with pytest.raises(ValidationError, match="activation readiness failed"):
            cp.activate_work_package(
                package_id,
                expected_plan_version=1,
                expected_epoch=1,
                actor="operator",
            )
        held = cp.work_package_telemetry_export(
            package_id=package_id, station="admission"
        )["station_attempts"][-1]
        assert held["terminal_status"] == "held"
        assert held["failure_class"] == "activation_hold"
        assert held["detail"]["readiness_blockers"]

        started = utcnow()
        completed = utcnow()
        report = {
            "schema": "mac.work_package.pipeline_run.v1",
            "run_id": "wppipe_telemetry_test",
            "status": "partial_failure",
            "started_at": started,
            "completed_at": completed,
            "outcomes": [
                {
                    "package_id": package_id,
                    "station": "integration_assembly",
                    "status": "busy",
                    "attempted": True,
                    "code": "station_busy",
                    "started_at": started,
                    "completed_at": completed,
                    "detail": {"error_type": "IntegrationBusy"},
                },
                {
                    "package_id": package_id,
                    "station": "landing",
                    "status": "failed",
                    "attempted": True,
                    "code": "station_failed",
                    "started_at": started,
                    "completed_at": completed,
                    "detail": {
                        "error_type": "StaleLandingBase",
                        "reason": "canonical ref moved",
                    },
                },
            ],
        }
        first = cp.record_work_package_pipeline_telemetry(report)
        second = cp.record_work_package_pipeline_telemetry(report)
        assert [row["id"] for row in second] == [row["id"] for row in first]

        exported = cp.work_package_telemetry_export(package_id=package_id)
        by_station = {row["station"]: row for row in exported["station_attempts"]}
        assert by_station["integration"]["terminal_status"] == "busy"
        assert by_station["integration"]["failure_class"] == "contention"
        assert by_station["landing"]["terminal_status"] == "stale"
        assert by_station["landing"]["failure_class"] == "stale_landing"
        assert all(row["queue_duration_ms"] >= 0 for row in by_station.values())
        assert all(row["execution_duration_ms"] >= 0 for row in by_station.values())
        assert exported["limitations"]

        with pytest.raises(StoreError, match="append-only"):
            cp.store.execute(
                "DELETE FROM work_package_station_attempts WHERE id = ?",
                (first[0]["id"],),
            )

        history_events = cp.list_events(
            subject_type="work_package", subject_id=package_id, limit=100
        )
        assert any(
            event["event_type"] == "work_package.admitted" for event in history_events
        )
        assert any(
            event["event_type"] == "work_package.station.landing.stale"
            for event in history_events
        )
    finally:
        cp.store.close()


def test_historical_backfill_records_route_but_refuses_to_invent_eligibility(
    tmp_path,
) -> None:
    _shared_dsn = ephemeral_dsn()
    db = ephemeral_dsn()
    cp = ControlPlane(
        store=store_on(_shared_dsn, initialize=True),
        secret_key="test-key-with-enough-entropy-32+chars",
    )
    try:
        task = cp.create_task("Historical legacy task")
        eligible = cp.create_task("Historical eligible legacy task")
        cp.store.execute(
            "UPDATE tasks SET metadata = ? WHERE id = ?",
            (
                json_dumps(
                    {
                        "managed_fast_lane": {
                            "schema": "mac.managed_single_task.route.v1",
                            "activation": "legacy_compatibility",
                        }
                    }
                ),
                eligible.id,
            ),
        )
        drop_table_guards(cp.store, "execution_cohort_assignments")
        cp.store.execute(
            "DELETE FROM execution_cohort_assignments WHERE task_id IN (?, ?)",
            (task.id, eligible.id),
        )
        drop_table_guards(cp.store, "telemetry_data_migrations")
        cp.store.execute(
            "DELETE FROM telemetry_data_migrations WHERE version = ?",
            ("execution_cohort_historical_backfill_v2",),
        )
    finally:
        cp.store.close()

    reopened = store_on(_shared_dsn, initialize=True)
    try:
        row = reopened.query_one(
            "SELECT * FROM execution_cohort_assignments WHERE task_id = ?", (task.id,)
        )
        assert row["treatment_route"] == "legacy_async"
        assert row["eligibility"] == "unknown"
        assert row["reason"] == "historical_absence_of_package_linkage"
        eligible_row = reopened.query_one(
            "SELECT * FROM execution_cohort_assignments WHERE task_id = ?",
            (eligible.id,),
        )
        assert eligible_row["treatment_route"] == "legacy_async"
        assert eligible_row["eligibility"] == "unknown"
        assert eligible_row["reason"] == "historical_control_plane_route_projection"
        assert json.loads(eligible_row["detail"])["shape_eligibility_source"] == (
            "control_plane_managed_fast_lane_projection"
        )
        marker = reopened.query_one(
            "SELECT * FROM telemetry_data_migrations WHERE version = ?",
            ("execution_cohort_historical_backfill_v2",),
        )
        assert marker is not None

        # Once marked, startup must not rescan historical catalogs.  Deleting a
        # fixture assignment after disabling its append-only guard makes a
        # second scan observable without relying on timing.
        drop_table_guards(reopened, "execution_cohort_assignments")
        reopened.execute(
            "DELETE FROM execution_cohort_assignments WHERE task_id = ?",
            (task.id,),
        )
    finally:
        reopened.close()

    no_rescan = store_on(_shared_dsn, initialize=True)
    try:
        assert (
            no_rescan.query_one(
                "SELECT 1 FROM execution_cohort_assignments WHERE task_id = ?",
                (task.id,),
            )
            is None
        )
        assert (
            no_rescan.query_one(
                "SELECT 1 FROM telemetry_data_migrations WHERE version = ?",
                ("execution_cohort_historical_backfill_v2",),
            )
            is not None
        )
    finally:
        no_rescan.close()


def test_historical_package_mode_is_unknown_without_finalization_receipt(
    tmp_path,
) -> None:
    _shared_dsn = ephemeral_dsn()
    db = ephemeral_dsn()
    cp = ControlPlane(
        store=store_on(_shared_dsn, initialize=True),
        secret_key="test-key-with-enough-entropy-32+chars",
    )
    try:
        _register_repository(cp)
        cp.work_packages.repository_verifier = _Verifier()
        package_id = cp.work_packages.admit(
            _plan(), actor="planner", reason="pre-instrumentation package"
        ).package.id

        # Convert the fixture into a pre-migration authority while retaining the
        # package itself.  No publication finalization exists, so linkage to a
        # work package alone cannot prove synchronized execution.
        drop_table_guards(cp.store, "work_package_station_attempts")
        cp.store.execute(
            "DELETE FROM work_package_station_attempts WHERE package_id = ?",
            (package_id,),
        )
        drop_table_guards(cp.store, "execution_cohort_assignments")
        cp.store.execute(
            "DELETE FROM execution_cohort_assignments WHERE package_id = ?",
            (package_id,),
        )
        drop_table_guards(cp.store, "telemetry_data_migrations")
        cp.store.execute(
            "DELETE FROM telemetry_data_migrations WHERE version = ?",
            ("execution_cohort_historical_backfill_v2",),
        )
    finally:
        cp.store.close()

    reopened = store_on(_shared_dsn, initialize=True)
    try:
        assignment = reopened.query_one(
            "SELECT * FROM execution_cohort_assignments WHERE package_id = ?",
            (package_id,),
        )
        assert assignment["eligibility"] == "unknown"
        assert assignment["treatment_route"] == "unknown_managed_mode"
        assert assignment["reason"] == "historical_package_mode_unproven"
        detail = json.loads(assignment["detail"])
        assert detail["route_source"] == "unavailable"
        assert detail["route_receipt_id"] == ""
    finally:
        reopened.close()
def test_preliminary_package_cohort_is_repaired_when_v2_marker_already_exists(
    tmp_path,
) -> None:
    _shared_dsn = ephemeral_dsn()
    db = ephemeral_dsn()
    cp = ControlPlane(
        store=store_on(_shared_dsn, initialize=True),
        secret_key="test-key-with-enough-entropy-32+chars",
    )
    try:
        _register_repository(cp)
        cp.work_packages.repository_verifier = _Verifier()
        package_id = cp.work_packages.admit(
            _plan(), actor="planner", reason="preliminary cohort"
        ).package.id

        drop_table_guards(cp.store, "execution_cohort_assignments")
        cp.store.execute(
            "UPDATE execution_cohort_assignments "
            "SET eligibility = ?, treatment_route = ?, cohort_key = ?, "
            "reason = ?, detail = ? WHERE package_id = ?",
            (
                "eligible",
                "managed_synchronized",
                "preliminary-managed",
                "preliminary-package-linkage",
                json_dumps({"schema": "mac.execution_cohort.prospective.v1"}),
                package_id,
            ),
        )
        drop_table_guards(cp.store, "telemetry_data_migrations")
        cp.store.execute(
            "DELETE FROM telemetry_data_migrations WHERE version = ?",
            ("execution_cohort_preliminary_package_repair_v3",),
        )
        assert cp.store.query_one(
            "SELECT 1 FROM telemetry_data_migrations WHERE version = ?",
            ("execution_cohort_historical_backfill_v2",),
        )
    finally:
        cp.store.close()

    repaired = store_on(_shared_dsn, initialize=True)
    try:
        assignment = repaired.query_one(
            "SELECT * FROM execution_cohort_assignments WHERE package_id = ?",
            (package_id,),
        )
        assert assignment["eligibility"] == "unknown"
        assert assignment["treatment_route"] == "unknown_managed_mode"
        assert assignment["reason"] == "historical_package_mode_unproven"
        assert json.loads(assignment["detail"]) == {
            "schema": "mac.execution_cohort.backfill.v2",
            "eligibility_source": "unavailable",
            "route_source": "unavailable",
            "route_receipt_id": "",
        }
        assert repaired.query_one(
            "SELECT 1 FROM telemetry_data_migrations WHERE version = ?",
            ("execution_cohort_preliminary_package_repair_v3",),
        )
        with pytest.raises(StoreError, match="immutable"):
            repaired.execute(
                "UPDATE execution_cohort_assignments SET reason = ? WHERE package_id = ?",
                ("changed", package_id),
            )
    finally:
        repaired.close()


def test_atomic_cohort_assignment_is_keyed_versioned_and_operator_independent() -> None:
    key = b"stable-test-randomization-key-32-bytes-minimum"
    first = deterministic_cohort_assignment(
        key=key,
        unit_id="task_0123456789abcdef0123456789abcdef",
        rollout_revision=7,
        treatment_percentage=50,
    )
    retry = deterministic_cohort_assignment(
        key=key,
        unit_id="task_0123456789abcdef0123456789abcdef",
        rollout_revision=7,
        treatment_percentage=50,
    )
    next_revision = deterministic_cohort_assignment(
        key=key,
        unit_id="task_0123456789abcdef0123456789abcdef",
        rollout_revision=8,
        treatment_percentage=50,
    )

    assert first == retry
    assert first["algorithm"] == "hmac_sha256_bucket_v1"
    assert first["randomization_unit"] == "task_id"
    assert 0 <= first["bucket_basis_points"] < 10_000
    assert first["allocation_fingerprint"] != next_revision["allocation_fingerprint"]
    assert "key" not in first
    assert "seed" not in first


def test_primary_auto_policy_is_randomized_while_operator_policy_is_excluded() -> None:
    cp = ControlPlane.in_memory()
    try:
        cp._execution_cohort_treatment_percentage = 0
        shape = {"eligible": True, "blockers": [], "repository_id": "repo"}
        rollout = {"ready": True, "revision": 4}
        auto = cp._single_task_cohort_assignment(
            task_id="task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            fast_lane_shape=shape,
            publication_policy="auto",
            rollout=rollout,
        )
        forced = cp._single_task_cohort_assignment(
            task_id="task_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            fast_lane_shape=shape,
            publication_policy="managed",
            rollout=rollout,
        )

        assert auto["eligibility"] == "eligible"
        assert auto["treatment_route"] == "legacy_async"
        assert auto["reason"] == "concurrent_exogenous_assignment"
        assert auto["detail"]["primary_analysis_eligible"] is True
        assert forced["eligibility"] == "ineligible"
        assert forced["treatment_route"] == "managed_synchronized"
        assert forced["reason"] == "operator_policy_excluded_primary_cohort"
        assert forced["detail"]["primary_analysis_eligible"] is False
        config = cp.store.query_one(
            "SELECT * FROM execution_cohort_configurations WHERE rollout_revision = ?",
            (cp._execution_cohort_revision,),
        )
        assert config["treatment_percentage"] == 0
        assert str(config["assignment_key_fingerprint"]).startswith("sha256:")

        cp._execution_cohort_treatment_percentage = 10
        with pytest.raises(ValidationError, match="immutable revision"):
            cp._single_task_cohort_assignment(
                task_id="task_cccccccccccccccccccccccccccccccc",
                fast_lane_shape=shape,
                publication_policy="auto",
                rollout=rollout,
            )
    finally:
        cp.store.close()


def test_control_plane_restart_rejects_existing_revision_with_different_seed(
    tmp_path, monkeypatch
) -> None:
    _shared_dsn = ephemeral_dsn()
    db = ephemeral_dsn()
    monkeypatch.setenv(
        "MAC_EXECUTION_COHORT_SEED", "first-stable-cohort-seed-with-32-bytes"
    )
    first = ControlPlane(
        store_on(_shared_dsn, initialize=True),
        secret_key="restart-test-control-plane-secret-32-bytes",
    )
    try:
        first._single_task_cohort_assignment(
            task_id="task_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            fast_lane_shape={"eligible": True, "blockers": []},
            publication_policy="auto",
            rollout={"ready": True, "revision": 1},
        )
    finally:
        first.store.close()

    monkeypatch.setenv(
        "MAC_EXECUTION_COHORT_SEED", "second-stable-cohort-seed-with-32-byte"
    )
    restarted_store = store_on(_shared_dsn, initialize=True)
    try:
        with pytest.raises(ValidationError, match="immutable revision"):
            ControlPlane(
                restarted_store,
                secret_key="restart-test-control-plane-secret-32-bytes",
            )
    finally:
        restarted_store.close()


def test_zero_percent_primary_assignment_creates_concurrent_legacy_control(
    monkeypatch,
) -> None:
    cp = ControlPlane.in_memory()
    try:
        _register_repository(cp)
        cp._execution_cohort_treatment_percentage = 0
        monkeypatch.setattr(
            cp,
            "_managed_single_task_rollout",
            lambda: {"ready": True, "revision": 9, "blockers": []},
        )
        task = cp.create_task(
            "Concurrent atomic control",
            project="telemetry",
            metadata={
                "no_decompose": True,
                "execution_contract": {
                    "schema": "mac.task_execution_contract.v1",
                    "type": "repository",
                    "quality": "strong",
                    "evidence_type": "repo_change",
                    "repository_id": "projectrepo_telemetry",
                    "repository_contract": {
                        "schema": "mac.project.repository_contract.v1"
                    },
                },
            },
        )
        assignment = cp.store.query_one(
            "SELECT * FROM execution_cohort_assignments WHERE task_id = ?",
            (task.id,),
        )
        detail = json.loads(assignment["detail"])

        assert assignment["eligibility"] == "eligible"
        assert assignment["treatment_route"] == "legacy_async"
        assert assignment["assigned_by"] == "execution-cohort-controller"
        assert detail["primary_analysis_eligible"] is True
        assert detail["randomization"]["treatment_percentage"] == 0
        assert cp.task_publication_route(task.id)["lane"] == "legacy"
        projected = cp.comparable_atomic_execution_outcomes()
        assert [row["task_id"] for row in projected] == [task.id]
        assert projected[0]["treatment_route"] == "legacy_async"
        assert projected[0]["canonical_publication_outcome"] == "censored"
        assert projected[0]["canonical_publication_terminal"] is False
        assert projected[0]["secondary_task_metrics"]["terminal"] is False
    finally:
        cp.store.close()


def test_primary_assignment_survives_treatment_specific_materialization_failure(
    monkeypatch,
) -> None:
    cp = ControlPlane.in_memory()
    try:
        _register_repository(cp)
        cp._execution_cohort_treatment_percentage = 100
        monkeypatch.setattr(
            cp,
            "_managed_single_task_rollout",
            lambda: {"ready": True, "revision": 9, "blockers": []},
        )
        monkeypatch.setattr(
            cp,
            "_managed_single_task_readiness",
            lambda **_kwargs: {
                "ready": True,
                "repository_id": "projectrepo_telemetry",
                "eligible_agent_ids": ["agent-ready"],
                "blockers": [],
            },
        )

        def fail_managed_materialization(**_kwargs):
            raise RuntimeError("remote base attestation failed")

        monkeypatch.setattr(
            cp, "_create_managed_single_task", fail_managed_materialization
        )
        task_id = "task_dddddddddddddddddddddddddddddddd"
        with pytest.raises(RuntimeError, match="base attestation failed"):
            cp.create_task(
                "Treatment-assigned materialization failure",
                project="telemetry",
                metadata={
                    "no_decompose": True,
                    "execution_contract": {
                        "schema": "mac.task_execution_contract.v1",
                        "type": "repository",
                        "quality": "strong",
                        "evidence_type": "repo_change",
                        "repository_id": "projectrepo_telemetry",
                        "repository_contract": {
                            "schema": "mac.project.repository_contract.v1"
                        },
                    },
                },
                _task_id=task_id,
            )

        assert cp.store.query_one("SELECT 1 FROM tasks WHERE id = ?", (task_id,)) is None
        assignment = cp.store.query_one(
            "SELECT * FROM execution_cohort_assignments WHERE task_id = ?",
            (task_id,),
        )
        assert assignment is not None
        assert assignment["eligibility"] == "eligible"
        assert assignment["treatment_route"] == "managed_synchronized"
        projected = cp.comparable_atomic_execution_outcomes()
        assert [row["task_id"] for row in projected] == [task_id]
        assert projected[0]["task_materialized"] is False
        assert projected[0]["canonical_publication_outcome"] == "censored"
        assert projected[0]["canonical_publication_terminal"] is False
        assert projected[0]["censoring_reason"] == "managed_package_not_materialized"
        assert projected[0]["secondary_task_metrics"]["state"] is None
        assert projected[0]["secondary_task_metrics"]["terminal"] is False
        assert projected[0]["secondary_task_metrics"]["attempt_count"] is None
    finally:
        cp.store.close()


def test_legacy_primary_success_requires_publication_and_canonical_proof() -> None:
    cp = ControlPlane.in_memory()
    try:
        completed_at = "2026-07-17T12:00:00.000000Z"
        task_id = "task_legacyproof000000000000000000000"
        _primary_task(
            cp,
            task_id=task_id,
            treatment_route="legacy_async",
            state="completed",
            completed_at=completed_at,
        )
        cp.store.execute(
            "INSERT INTO publications ("
            "id, task_id, target, status, evidence_id, content_hash, "
            "created_by, created_at"
            ") VALUES (?, ?, ?, 'published', NULL, NULL, 'test', ?)",
            ("pub_primary_legacy", task_id, "git://main", completed_at),
        )

        without_proof = cp.comparable_atomic_execution_outcomes()[0]
        assert without_proof["schema"] == (
            "mac.execution_cohort.comparable_atomic_outcome.v2"
        )
        assert without_proof["canonical_publication_outcome"] == "failed"
        assert without_proof["canonical_publication_success"] is False
        assert without_proof["canonical_publication_failure_class"] == (
            "legacy_task_completed_without_publication_proof"
        )

        tip = "b" * 40
        cp.store.execute(
            "INSERT INTO evidence ("
            "id, task_id, kind, uri, summary, checksum, metadata, created_by, created_at"
            ") VALUES (?, ?, 'test', ?, 'canonical proof', NULL, ?, 'controller', ?)",
            (
                "ev_primary_canonical",
                task_id,
                "ledger://canonical-integration/%s/%s" % (task_id, tip),
                json_dumps(
                    {
                        "verification": {
                            "canonical_integration": {
                                "schema": "mac.canonical_integration.v1",
                                "status": "pass",
                                "canonical_tip_sha": tip,
                                "contains_reviewed_head": True,
                                "remote_verified": True,
                            }
                        }
                    }
                ),
                completed_at,
            ),
        )
        with_proof = cp.comparable_atomic_execution_outcomes()[0]
        assert with_proof["canonical_publication_outcome"] == "succeeded"
        assert with_proof["canonical_publication_success"] is True
        assert with_proof["canonical_publication_proof"] == {
            "type": "legacy_publication_receipt",
            "id": "pub_primary_legacy",
            "target": "git://main",
            "canonical_integration_evidence_id": "ev_primary_canonical",
            "canonical_tip_sha": tip,
        }
        assert with_proof["primary_estimand"] == (
            "intention_to_treat_canonical_publication_outcome"
        )
        assert with_proof["secondary_task_metrics"]["role"] == (
            "secondary_route_internal"
        )
    finally:
        cp.store.close()


@pytest.mark.parametrize("terminal_state", ["failed", "cancelled"])
def test_managed_package_failure_and_cancellation_are_primary_terminal_failures(
    terminal_state: str,
) -> None:
    cp = ControlPlane.in_memory()
    try:
        _register_repository(cp)
        cp.work_packages.repository_verifier = _Verifier()
        package_id = _managed_primary_package(
            cp,
            task_id="task_managed%s000000000000000000000" % terminal_state,
            package_id="wp_primary_%s" % terminal_state,
        )
        terminal_at = "2026-07-17T13:00:00.000000Z"
        cp.store.execute(
            "UPDATE work_packages SET state = ?, completed_at = ?, updated_at = ? "
            "WHERE id = ?",
            (terminal_state, terminal_at, terminal_at, package_id),
        )

        outcome = cp.comparable_atomic_execution_outcomes()[0]
        assert outcome["canonical_publication_outcome"] == "failed"
        assert outcome["canonical_publication_terminal"] is True
        assert outcome["canonical_publication_success"] is False
        assert outcome["canonical_publication_failure_class"] == (
            "managed_package_%s" % terminal_state
        )
        assert outcome[
            "assignment_to_canonical_publication_terminal_duration_ms"
        ] == 14_400_000
    finally:
        cp.store.close()


def test_recoverable_pause_is_censored_but_exhausted_rework_is_terminal() -> None:
    cp = ControlPlane.in_memory()
    try:
        _register_repository(cp)
        cp.work_packages.repository_verifier = _Verifier()
        package_id = _managed_primary_package(
            cp,
            task_id="task_managedpause00000000000000000000",
            package_id="wp_primary_pause",
            task_state="completed",
            task_completed_at="2026-07-17T11:00:00.000000Z",
        )
        paused_at = "2026-07-17T12:00:00.000000Z"
        cp.store.execute(
            "UPDATE work_packages SET state = 'paused', updated_at = ? WHERE id = ?",
            (paused_at, package_id),
        )

        recoverable = cp.comparable_atomic_execution_outcomes()[0]
        assert recoverable["canonical_publication_outcome"] == "censored"
        assert recoverable["canonical_publication_success"] is None
        assert recoverable["secondary_task_metrics"]["success"] is True

        cp.store.execute(
            "INSERT INTO work_package_history ("
            "id, package_id, seq, event_type, actor, plan_version, epoch, detail, created_at"
            ") VALUES (?, ?, ?, ?, 'acceptance-controller', 1, 1, ?, ?)",
            (
                "wph_primary_exhausted",
                package_id,
                int(
                    cp.store.query_one(
                        "SELECT COALESCE(MAX(seq), 0) + 1 AS seq "
                        "FROM work_package_history WHERE package_id = ?",
                        (package_id,),
                    )["seq"]
                ),
                "work_package.candidate_rejected",
                json_dumps(
                    {
                        "schema": "mac.work_package.candidate_rejection.v1",
                        "candidate_id": "wpcandidate_primary_exhausted",
                        "retry_staged": False,
                        "remaining_rework_cycles": 0,
                    }
                ),
                paused_at,
            ),
        )
        exhausted = cp.comparable_atomic_execution_outcomes()[0]
        assert exhausted["canonical_publication_outcome"] == "failed"
        assert exhausted["canonical_publication_failure_class"] == (
            "managed_candidate_rework_exhausted"
        )
        assert exhausted["canonical_publication_proof"]["id"] == (
            "wph_primary_exhausted"
        )
    finally:
        cp.store.close()


def test_root_task_lineage_does_not_rewrite_its_immutable_task_cohort() -> None:
    cp = ControlPlane.in_memory()
    try:
        _register_repository(cp)
        cp.work_packages.repository_verifier = _Verifier()
        root = cp.create_task("Existing legacy root")
        before = cp.store.query_one(
            "SELECT id, treatment_route FROM execution_cohort_assignments "
            "WHERE task_id = ?",
            (root.id,),
        )

        admitted = cp.work_packages.admit(
            _plan(),
            actor="planner",
            reason="root is lineage only",
            root_task_id=root.id,
        )
        task_assignment = cp.store.query_one(
            "SELECT id, treatment_route, package_id FROM execution_cohort_assignments "
            "WHERE task_id = ?",
            (root.id,),
        )
        package_assignment = cp.store.query_one(
            "SELECT task_id, treatment_route FROM execution_cohort_assignments "
            "WHERE package_id = ?",
            (admitted.package.id,),
        )

        assert task_assignment["id"] == before["id"]
        assert task_assignment["treatment_route"] == "legacy_async"
        assert task_assignment["package_id"] is None
        assert package_assignment["task_id"] is None
        assert package_assignment["treatment_route"] == "managed_synchronized"
    finally:
        cp.store.close()


def test_controller_ledger_keeps_global_complete_generic_and_unmapped_outcomes() -> (
    None
):
    cp = ControlPlane.in_memory()
    try:
        _register_repository(cp)
        cp.work_packages.repository_verifier = _Verifier()
        package_id = cp.work_packages.admit(
            _plan(), actor="planner", reason="controller coverage"
        ).package.id
        started = "2026-07-17T12:00:00+01:00"
        completed = "2026-07-17T11:00:01Z"
        outcomes = [
            {
                "package_id": "",
                "station": "inventory",
                "status": "failed",
                "attempted": False,
                "code": "inventory_failed",
            },
            {
                "package_id": package_id,
                "plan_version": 1,
                "epoch": 1,
                "station": "complete",
                "status": "no_op",
                "attempted": False,
                "code": "batch_terminal",
            },
            {
                "package_id": package_id,
                "plan_version": 1,
                "epoch": 1,
                "station": "certification",
                "status": "failed",
                "attempted": False,
                "code": "unknown_certification_job_state",
            },
            {
                "package_id": package_id,
                "plan_version": 1,
                "epoch": 1,
                "station": "future_station",
                "status": "failed",
                "attempted": False,
                "code": "future_failure",
            },
        ]
        cp.record_work_package_pipeline_telemetry(
            {
                "run_id": "wppipe_complete_coverage",
                "status": "partial_failure",
                "started_at": started,
                "completed_at": completed,
                "outcomes": outcomes,
            }
        )

        raw = cp.store.query_all(
            "SELECT * FROM work_package_controller_outcomes "
            "WHERE pipeline_run_id = ? ORDER BY outcome_index",
            ("wppipe_complete_coverage",),
        )
        assert [row["outcome_index"] for row in raw] == [-1, 0, 1, 2, 3]
        assert raw[1]["package_id"] == ""
        assert raw[-1]["operation"] == "future_station"
        assert raw[-1]["failure_class"] == "unmapped_controller_operation"
        assert all(str(row["started_at"]).endswith("Z") for row in raw)

        attempts = cp.work_package_telemetry_export(package_id=package_id)[
            "station_attempts"
        ]
        operations = {row["operation"]: row for row in attempts}
        assert operations["complete"]["station"] == "controller"
        assert operations["certification"]["station"] == "certification"
        assert operations["future_station"]["failure_class"] == (
            "unmapped_controller_operation"
        )
    finally:
        cp.store.close()


def test_observer_writes_measurement_when_log_fails_and_alerts_when_measurement_fails(
    monkeypatch,
) -> None:
    cp = ControlPlane.in_memory()
    try:
        report = {
            "run_id": "wppipe_observer_independence",
            "status": "completed",
            "started_at": utcnow(),
            "completed_at": utcnow(),
            "outcomes": [],
        }
        monkeypatch.setattr(cp, "record_log", lambda *_args, **_kwargs: 1 / 0)
        observe = control_plane_pipeline_observer(cp)
        observe(report)
        assert (
            cp.store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_controller_outcomes "
                "WHERE pipeline_run_id = ?",
                (report["run_id"],),
            )["n"]
            == 1
        )

        def fail_measurement(_report):
            raise RuntimeError("token=must-not-be-persisted")

        monkeypatch.setattr(
            cp, "record_work_package_pipeline_telemetry", fail_measurement
        )
        observe({**report, "run_id": "wppipe_observer_failure"})
        health = cp.work_package_telemetry_export()["measurement_health"]
        assert health["failure_count"] == 1
        assert health["alert"] is True
        assert health["last_error_type"] == "RuntimeError"
        assert "must-not-be-persisted" not in json.dumps(health)
    finally:
        cp.store.close()


def test_route_filtered_export_does_not_leak_other_cohort_finalization_outcomes() -> (
    None
):
    cp = ControlPlane.in_memory()
    try:
        cp.work_package_telemetry.assign_cohort(
            task_id=None,
            package_id="wp_filter_managed",
            eligibility="eligible",
            treatment_route="managed_synchronized",
            rollout_revision=1,
            cohort_key="filter-managed",
            reason="test",
            actor="test",
        )
        cp.work_package_telemetry.assign_cohort(
            task_id=None,
            package_id="wp_filter_legacy",
            eligibility="eligible",
            treatment_route="legacy_async",
            rollout_revision=1,
            cohort_key="filter-legacy",
            reason="test",
            actor="test",
        )
        now = "2026-07-17T12:00:00.000000Z"
        # The parent finalization/package rows are irrelevant to what this
        # asserts (export filtering); building the real chain -- batches,
        # landing receipts, repositories, certifications -- would test the
        # fixture instead of the filter.
        with cp.store.foreign_keys_suspended() as unchecked:
            unchecked.execute(
                "INSERT INTO work_package_finalization_outcomes ("
                "id, finalization_id, package_id, outcome_type, external_id, "
                "observed_at, actor, detail, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?), (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "outcome_filter_managed",
                    "finalization_filter_managed",
                    "wp_filter_managed",
                    "incident",
                    "incident-managed",
                    now,
                    "test",
                    "{}",
                    now,
                    "outcome_filter_legacy",
                    "finalization_filter_legacy",
                    "wp_filter_legacy",
                    "incident",
                    "incident-legacy",
                    now,
                    "test",
                    "{}",
                    now,
                ),
            )

        managed = cp.work_package_telemetry_export(
            treatment_route="managed_synchronized", eligibility="eligible"
        )
        legacy = cp.work_package_telemetry_export(
            treatment_route="legacy_async", eligibility="eligible"
        )
        assert [row["id"] for row in managed["finalization_outcomes"]] == [
            "outcome_filter_managed"
        ]
        assert [row["id"] for row in legacy["finalization_outcomes"]] == [
            "outcome_filter_legacy"
        ]
    finally:
        cp.store.close()


def test_package_filtered_export_does_not_include_other_primary_task_outcomes() -> None:
    cp = ControlPlane.in_memory()
    try:
        now = "2026-07-17T12:00:00.000000Z"
        task_ids = (
            "task_11111111111111111111111111111111",
            "task_22222222222222222222222222222222",
        )
        package_ids = ("wp_filter_primary_one", "wp_filter_primary_two")
        for task_id, package_id in zip(task_ids, package_ids):
            cp.store.execute(
                "INSERT INTO tasks ("
                "id, title, description, priority, state, required_capabilities, "
                "dependencies, metadata, attempt_count, max_attempts, created_at, updated_at"
                ") VALUES (?, ?, '', 0, 'open', '[]', '[]', '{}', 0, 3, ?, ?)",
                (task_id, task_id, now, now),
            )
            cp.work_package_telemetry.assign_cohort(
                task_id=task_id,
                package_id=None,
                eligibility="eligible",
                treatment_route="legacy_async",
                rollout_revision=1,
                cohort_key="package-filter-control",
                reason="concurrent_exogenous_assignment",
                actor="test",
                assigned_at=now,
                detail={
                    "schema": "mac.execution_cohort.prospective.v3",
                    "primary_analysis_eligible": True,
                    "estimand": (
                        "intention_to_treat_canonical_publication_outcome"
                    ),
                    "randomization": {
                        "algorithm": "hmac_sha256_bucket_v1",
                        "treatment_route": "legacy_async",
                    },
                },
            )
            cp.store.execute(
                "INSERT INTO work_packages ("
                "id, root_task_id, goal, state, current_plan_version, current_epoch, "
                "metadata, created_by, created_at, updated_at"
                ") VALUES (?, ?, ?, 'draft', 0, 0, '{}', 'test', ?, ?)",
                (package_id, task_id, package_id, now, now),
            )

        exported = cp.work_package_telemetry_export(package_id=package_ids[0])
        assert [
            row["task_id"] for row in exported["comparable_atomic_outcomes"]
        ] == [task_ids[0]]
    finally:
        cp.store.close()


def test_station_timestamps_are_canonical_and_clock_clamps_are_explicit() -> None:
    cp = ControlPlane.in_memory()
    try:
        _register_repository(cp)
        cp.work_packages.repository_verifier = _Verifier()
        package_id = cp.work_packages.admit(
            _plan(), actor="planner", reason="clock clamp"
        ).package.id
        attempt = cp.work_package_telemetry.record_station_attempt(
            package_id=package_id,
            station="controller",
            operation="clock_probe",
            attempted=False,
            terminal_status="held",
            queued_at="2026-07-17T13:00:00+00:00",
            started_at="2026-07-17T12:00:00Z",
            completed_at="2026-07-17T11:00:00-01:00",
            actor="test",
            pipeline_run_id="clock-clamp-probe",
            reason_code="clock_probe",
        )

        assert attempt["queued_at"] == "2026-07-17T12:00:00.000000Z"
        assert attempt["started_at"] == "2026-07-17T12:00:00.000000Z"
        assert attempt["completed_at"] == "2026-07-17T12:00:00.000000Z"
        assert attempt["queue_duration_ms"] == 0
        assert attempt["execution_duration_ms"] == 0
        assert attempt["detail"]["clock_clamps"][0]["field"] == "queued_at"
    finally:
        cp.store.close()
