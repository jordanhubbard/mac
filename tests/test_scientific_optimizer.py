from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mac.api import create_app
from mac.cli import main
from mac.models import NotFoundError, ValidationError, utcnow
from mac.scientific_optimizer import (
    ScientificOptimizerConfig,
    ScientificOptimizerService,
    _bounded_float,
    _bounded_int,
    _parse_time,
    derive_task_kpis,
    estimate_route_cost,
    validate_policy_parameters,
)
from mac.services import ControlPlane


def _completed_experiment() -> tuple[ControlPlane, dict, dict, dict]:
    cp = ControlPlane.in_memory()
    optimizer = cp.optimizer
    control = optimizer.create_policy("baseline", "demo", {})
    optimizer.promote_policy(control["id"], actor="test")
    treatment = optimizer.create_policy("plan-first", "demo", {"plan_first": True})
    experiment = optimizer.create_experiment(
        "reduce-rework",
        "demo",
        "Planning first reduces rework without reducing accepted quality.",
        control["id"],
        treatment["id"],
        primary_metric="cycles_to_accept",
        min_samples_per_arm=2,
        max_samples_per_arm=2,
        exploration_fraction=1.0,
        outcome_horizon_seconds=0,
        auto_promote=False,
    )
    optimizer.start_experiment(experiment["id"], actor="test")

    arm_counts = {"control": 0, "treatment": 0}
    for index in range(30):
        task = cp.create_task(
            "experiment task %d" % index,
            project="demo",
            metadata={
                "execution_contract": {"type": "repository", "quality": "strong"}
            },
        )
        assignment = cp.store.query_one(
            "SELECT arm FROM scientific_assignments WHERE task_id = ?", (task.id,)
        )
        assert assignment is not None
        arm = str(assignment["arm"])
        arm_counts[arm] += 1
        cp.store.execute(
            "UPDATE tasks SET state = 'completed', attempt_count = ?, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (3 if arm == "control" else 1, utcnow(), utcnow(), task.id),
        )
        if min(arm_counts.values()) >= 2:
            break
    assert min(arm_counts.values()) >= 2
    return cp, control, treatment, experiment


def test_policy_allowlist_cannot_change_safety_contract() -> None:
    assert (
        validate_policy_parameters(
            {
                "model_strength": 7,
                "review_max_iterations": 12,
                "plan_first": True,
                "review_mode": "blind",
            }
        )["model_strength"]
        == 7
    )
    with pytest.raises(ValidationError, match="non-allowlisted"):
        validate_policy_parameters({"required_checks": [], "sandbox_policy": "off"})


def test_kpis_capture_quality_cycles_latency_tokens_and_known_cost() -> None:
    detail = {
        "task": {
            "id": "task_1",
            "project": "demo",
            "state": "completed",
            "attempt_count": 2,
            "created_at": "2026-01-01T00:00:00+00:00",
            "completed_at": "2026-01-01T00:00:02+00:00",
            "metadata": {
                "review_outcomes": [{"kind": "clean_window", "status": "confirmed"}]
            },
        },
        "reviews": [{"status": "rejected"}, {"status": "approved"}],
        "publications": [{}],
    }
    metrics = derive_task_kpis(
        detail,
        [
            {
                "id": "route_1",
                "detail": {
                    "schema": "mac.llm_route.v1",
                    "input_tokens": 100,
                    "output_tokens": 25,
                    "duration_ms": 50,
                    "cost_usd": 0.0125,
                },
            }
        ],
    )
    assert metrics["accepted_success"] == 1.0
    assert metrics["delayed_quality_success"] == 1.0
    assert metrics["cycles_to_accept"] == 3.0
    assert metrics["lead_time_ms"] == 2000.0
    assert metrics["total_tokens"] == 125.0
    assert metrics["model_latency_ms"] == 50.0
    assert metrics["cost_known"] is True
    assert metrics["cost_usd"] == 0.0125


def test_route_cost_uses_native_models_catalog(monkeypatch) -> None:
    class CatalogModel:
        cost_input = 2.0
        cost_output = 8.0
        cost_cache_read = 0.5

        @staticmethod
        def has_cost_data() -> bool:
            return True

    from mac import models_catalog

    monkeypatch.setattr(models_catalog, "get_model_info", lambda *_args: CatalogModel())
    cost, known = estimate_route_cost(
        {
            "response_model": "openai/test-model",
            "provider": "openai",
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 100,
                "cached_tokens": 500,
            },
        }
    )
    assert known is True
    assert cost == pytest.approx(0.00205)


def test_experiment_assigns_tasks_and_requires_evidence_before_promotion() -> None:
    cp, control, treatment, experiment = _completed_experiment()
    optimizer = cp.optimizer

    optimizer.refresh_experiment(experiment["id"])
    decision = optimizer.analyze_experiment(experiment["id"], actor="test")
    assert decision["status"] == "promote"
    assert decision["comparison"]["difference"] == -2.0
    assert decision["guardrails_pass"] is True
    assert decision["inference"]["familywise_alpha"] == 0.05
    assert optimizer.get_experiment(experiment["id"])["state"] == "candidate"
    assert optimizer.active_policy("demo")["id"] == control["id"]

    promoted = optimizer.promote_experiment(
        experiment["id"], actor="test", reason="test evidence passed"
    )
    assert promoted["state"] == "monitoring"
    assert optimizer.active_policy("demo")["id"] == treatment["id"]

    task = cp.create_task(
        "monitor policy",
        project="demo",
        metadata={
            "optimizer_exempt": True,
            "execution_contract": {"type": "repository", "quality": "strong"},
        },
    )
    assert "scientific_optimizer" not in task.metadata
    evidence = optimizer.experiment_evidence(experiment["id"])
    assert evidence["assignments"]
    assert evidence["observations"]
    assert evidence["decisions"][-1]["status"] == "promote"
    assert any(
        event["event_type"] == "experiment.promoted_to_monitoring"
        for event in evidence["events"]
    )


def test_monitoring_rolls_back_a_primary_kpi_regression() -> None:
    cp, control, treatment, experiment = _completed_experiment()
    optimizer = cp.optimizer
    optimizer.refresh_experiment(experiment["id"])
    optimizer.analyze_experiment(experiment["id"], actor="test")
    optimizer.promote_experiment(experiment["id"], actor="test")

    for arm, policy, attempts in (
        ("control", control, 1),
        ("control", control, 1),
        ("treatment", treatment, 3),
        ("treatment", treatment, 3),
    ):
        task = cp.create_task(
            "monitor %s" % arm,
            project="demo",
            metadata={"optimizer_exempt": True},
        )
        assignment = {
            "experiment_id": experiment["id"],
            "task_id": task.id,
            "arm": arm,
            "policy_id": policy["id"],
            "phase": "monitor",
            "propensity": 0.5,
            "stratum": "test",
            "assigned_at": utcnow(),
        }
        with cp.store.transaction() as conn:
            optimizer.insert_assignment(conn, assignment)
        cp.store.execute(
            "UPDATE tasks SET state = 'completed', attempt_count = ?, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (attempts, utcnow(), utcnow(), task.id),
        )
    optimizer.refresh_experiment(experiment["id"])
    decision = optimizer.analyze_experiment(experiment["id"], actor="test")
    assert decision["status"] == "rollback"
    assert decision["primary_regressed"] is True


def test_cost_experiment_does_not_treat_unknown_price_as_zero() -> None:
    cp, _control, _treatment, experiment = _completed_experiment()
    cp.store.execute(
        "UPDATE scientific_experiments SET primary_metric = 'cost_usd' WHERE id = ?",
        (experiment["id"],),
    )
    cp.optimizer.refresh_experiment(experiment["id"])
    decision = cp.optimizer.analyze_experiment(experiment["id"])
    assert decision["status"] == "collecting"
    assert decision["sample_counts"] == {"control": 0, "treatment": 0}
    assert min(decision["validated_sample_counts"].values()) >= 2


def test_optimizer_api_and_cli_expose_durable_policy_crud(
    tmp_path, capsys, monkeypatch
) -> None:
    cp = ControlPlane.in_memory()
    with TestClient(create_app(control_plane=cp)) as client:
        created = client.post(
            "/optimizer/policies",
            json={
                "name": "baseline",
                "project": "demo",
                "parameters": {"model_strength": 8},
                "created_by": "test",
            },
        )
        assert created.status_code == 200
        policy_id = created.json()["id"]
        assert (
            client.post(
                "/optimizer/policies/%s/promote" % policy_id,
                json={"actor": "test", "reason": "baseline"},
            ).status_code
            == 200
        )
        assert client.get("/optimizer/status").json()["schema"].endswith(".v1")

    db_path = tmp_path / "optimizer.db"
    monkeypatch.setenv("MAC_SECRET_KEY", "test-secret-key-that-is-long-enough-1234")
    exit_code = main(
        [
            "--db",
            str(db_path),
            "optimizer",
            "policy",
            "create",
            "cli-baseline",
            "demo",
            "--parameters",
            json.dumps({"plan_first": True}),
            "--json",
        ]
    )
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out)["parameters"] == {"plan_first": True}


def test_optimizer_config_is_explicitly_enableable() -> None:
    config = ScientificOptimizerConfig.from_env(
        {
            "MAC_SCIENTIFIC_OPTIMIZER_ENABLED": "1",
            "MAC_SCIENTIFIC_OPTIMIZER_AUTO_PROPOSE": "0",
            "MAC_SCIENTIFIC_OPTIMIZER_INTERVAL_SECONDS": "30",
        }
    )
    assert config.active is True
    assert config.auto_propose is False
    assert config.interval_seconds == 30.0


def test_database_lease_prevents_duplicate_replica_ticks() -> None:
    cp = ControlPlane.in_memory()
    config = ScientificOptimizerConfig(
        enabled=True,
        interval_seconds=300,
        initial_delay_seconds=0,
        auto_propose=False,
    )
    first = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        config=config,
    )
    second = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        config=config,
    )
    assert first.tick()["status"] == "ok"
    blocked = second.tick()
    assert blocked["status"] == "busy"
    assert "replica" in blocked["reason"]


def test_autonomous_hypothesis_uses_measured_cost_and_strength_ladder(
    monkeypatch,
) -> None:
    cp = ControlPlane.in_memory()
    for index in range(2):
        task = cp.create_task(
            "baseline %d" % index,
            project="demo",
            metadata={
                "execution_contract": {"type": "repository", "quality": "strong"}
            },
        )
        cp.store.execute(
            "UPDATE tasks SET state = 'completed', attempt_count = 1, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (utcnow(), utcnow(), task.id),
        )
        cp.record_log(
            "llm.route",
            subject_type="task",
            subject_id=task.id,
            detail={
                "schema": "mac.llm_route.v1",
                "input_tokens": 100,
                "output_tokens": 20,
                "cost_usd": 0.01,
            },
        )
    optimizer = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        config=ScientificOptimizerConfig(
            min_baseline_tasks=2,
            default_min_samples_per_arm=2,
            default_max_samples_per_arm=2,
        ),
    )
    monkeypatch.setattr(optimizer, "_strength_ladder_ready", lambda: True)
    experiment = optimizer.propose_next_experiment("demo")
    assert experiment is not None
    assert experiment["state"] == "running"
    assert experiment["primary_metric"] == "cost_usd"
    treatment = optimizer.get_policy(experiment["treatment_policy_id"])
    assert treatment["parameters"] == {"model_strength": 9}


def test_autonomous_optimizer_files_deduplicated_dispatchable_improvement_work() -> (
    None
):
    cp = ControlPlane.in_memory()
    for index in range(2):
        task = cp.create_task(
            "rework baseline %d" % index,
            project="demo",
            metadata={
                "execution_contract": {"type": "repository", "quality": "strong"}
            },
        )
        cp.store.execute(
            "UPDATE tasks SET state = 'completed', attempt_count = 3, "
            "completed_at = ?, updated_at = ? WHERE id = ?",
            (utcnow(), utcnow(), task.id),
        )
    optimizer = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        create_task=cp.create_task,
        config=ScientificOptimizerConfig(min_baseline_tasks=2),
    )
    baseline = optimizer.create_policy(
        "plan-first", "demo", {"plan_first": True}, created_by="test"
    )
    optimizer.promote_policy(baseline["id"], actor="test")
    assert optimizer.propose_next_experiment("demo") is None

    proposal = optimizer.propose_improvement_task("demo")
    assert proposal is not None
    created = proposal["task"]
    assert created["state"] == "open"
    assert created["metadata"]["origin"]["type"] == "scientific_optimizer"
    assert created["metadata"]["optimizer_exempt"] is True
    assert "no_dispatch" not in created["metadata"]
    assert optimizer.propose_improvement_task("demo") is None
    cp.store.execute(
        "UPDATE tasks SET state = 'completed', completed_at = ?, updated_at = ? "
        "WHERE id = ?",
        (utcnow(), utcnow(), created["id"]),
    )
    assert optimizer.propose_improvement_task("demo") is None


def test_optimizer_validation_and_kpi_failure_edges() -> None:
    for value in ("nope", float("nan"), -1):
        with pytest.raises(ValidationError):
            _bounded_float(value, "value", 0, 1)
    for value in ("nope", 0, 11):
        with pytest.raises(ValidationError):
            _bounded_int(value, "value", 1, 10)
    with pytest.raises(ValidationError, match="must be an object"):
        validate_policy_parameters([])
    assert validate_policy_parameters({"model": "provider/model"}) == {
        "model": "provider/model"
    }
    for parameters in (
        {"model": ""},
        {"review_model": "x" * 257},
        {"model_strength": 11},
        {"max_iterations": 0},
        {"plan_first": "yes"},
        {"review_mode": "unsafe"},
    ):
        with pytest.raises(ValidationError):
            validate_policy_parameters(parameters)
    assert _parse_time("") is None
    assert _parse_time("not-a-date") is None
    assert _parse_time("2026-01-01T00:00:00Z") is not None

    metrics = derive_task_kpis(
        {
            "task": {
                "id": "task_failed",
                "project": "demo",
                "state": "failed",
                "created_at": "bad",
                "completed_at": "also-bad",
                "metadata": {
                    "review_outcomes": [
                        {
                            "kind": "escaped_defect",
                            "status": "confirmed",
                            "severity_weight": 3,
                        }
                    ]
                },
            },
            "reviews": [None, {"status": "rejected"}],
            "publications": [None],
        },
        [
            {"id": "duplicate", "detail": {"schema": "not-a-route"}},
            {"id": "duplicate", "detail": {"schema": "mac.llm_route.v1"}},
        ],
    )
    assert metrics["quality_source"] == "escaped_defect"
    assert metrics["quality_validated"] is True
    assert metrics["accepted_success"] == 0.0
    assert metrics["escaped_defect_severity"] == 3.0
    assert metrics["lead_time_ms"] == 0.0
    assert estimate_route_cost({"input_tokens": 100}) == (0.0, False)


def test_policy_and_experiment_lifecycle_rejects_invalid_protocols() -> None:
    cp = ControlPlane.in_memory()
    optimizer = cp.optimizer
    with pytest.raises(ValidationError, match="required"):
        optimizer.create_policy("", "demo", {})
    with pytest.raises(NotFoundError):
        optimizer.get_policy("policy_missing")
    control = optimizer.create_policy("baseline", "demo", {})
    version_two = optimizer.create_policy("baseline", "demo", {})
    treatment = optimizer.create_policy("treatment", "demo", {"plan_first": True})
    foreign = optimizer.create_policy("foreign", "other", {})
    assert version_two["version"] == 2
    assert len(optimizer.list_policies(project="demo", status="candidate")) == 3
    with pytest.raises(ValidationError, match="unsupported policy status"):
        optimizer.list_policies(status="unknown")
    with pytest.raises(ValidationError, match="different project"):
        optimizer.rollback_policy("demo", foreign["id"])
    optimizer.promote_policy(treatment["id"])
    assert optimizer.rollback_policy("demo", control["id"])["status"] == "active"

    def create(**overrides):
        values = {
            "name": "experiment",
            "project": "demo",
            "hypothesis": "a falsifiable hypothesis",
            "control_policy_id": control["id"],
            "treatment_policy_id": treatment["id"],
            "primary_metric": "cycles_to_accept",
            "min_samples_per_arm": 2,
            "max_samples_per_arm": 2,
        }
        values.update(overrides)
        return optimizer.create_experiment(**values)

    for overrides in (
        {"name": ""},
        {"primary_metric": "imaginary"},
        {"direction": "sideways"},
        {"treatment_policy_id": control["id"]},
        {"treatment_policy_id": foreign["id"]},
        {"min_samples_per_arm": 1},
        {"max_samples_per_arm": 1},
        {"exploration_fraction": 0},
        {"outcome_horizon_seconds": -1},
        {"guardrails": {"imaginary": {}}},
        {"guardrails": {"accepted_success": {"direction": "sideways"}}},
    ):
        with pytest.raises(ValidationError):
            create(**overrides)
    with pytest.raises(NotFoundError):
        optimizer.get_experiment("experiment_missing")
    with pytest.raises(ValidationError, match="unsupported experiment state"):
        optimizer.list_experiments(state="unknown")

    experiment = create()
    assert optimizer.list_experiments(project="demo")[0]["id"] == experiment["id"]
    with pytest.raises(ValidationError, match="only active"):
        optimizer.pause_experiment(experiment["id"])
    with pytest.raises(ValidationError, match="only be promoted"):
        optimizer.promote_experiment(experiment["id"])
    optimizer.promote_policy(treatment["id"])
    with pytest.raises(ValidationError, match="control policy"):
        optimizer.start_experiment(experiment["id"])
    optimizer.promote_policy(control["id"])
    optimizer.start_experiment(experiment["id"])
    with pytest.raises(ValidationError, match="only start"):
        optimizer.start_experiment(experiment["id"])
    with pytest.raises(ValidationError, match="evidence-backed"):
        optimizer.promote_experiment(experiment["id"])
    assert optimizer.pause_experiment(experiment["id"])["state"] == "paused"
    assert optimizer.start_experiment(experiment["id"])["state"] == "running"
    with pytest.raises(NotFoundError, match="not assigned"):
        optimizer.observe_task(experiment["id"], "task_missing")


def test_assignment_exclusions_and_active_blind_policy() -> None:
    cp = ControlPlane.in_memory()
    optimizer = cp.optimizer
    active = optimizer.create_policy(
        "blind", "demo", {"review_mode": "blind", "plan_first": True}
    )
    optimizer.promote_policy(active["id"])
    unchanged, assignment = optimizer.prepare_task_assignment("task_1", None, {})
    assert unchanged == {}
    assert assignment is None
    unchanged, assignment = optimizer.prepare_task_assignment(
        "task_2", "demo", {"optimizer_exempt": True}
    )
    assert assignment is None
    applied, assignment = optimizer.prepare_task_assignment("task_3", "demo", {})
    assert assignment is None
    assert applied["plan_first"] is True
    assert applied["review_experiment"]["blind"] is True

    treatment = optimizer.create_policy(
        "standard", "demo", {"review_mode": "standard", "plan_first": False}
    )
    experiment = optimizer.create_experiment(
        "assignment",
        "demo",
        "assignment exclusions",
        active["id"],
        treatment["id"],
        primary_metric="cycles_to_accept",
        min_samples_per_arm=2,
        max_samples_per_arm=2,
        exploration_fraction=0.01,
    )
    optimizer.start_experiment(experiment["id"])
    for index, metadata in enumerate(
        (
            {"origin": {"type": "scientific_optimizer"}},
            {"origin": {"type": "backlog_grooming"}},
            {"execution_contract": {"type": "operator"}},
            {
                "execution_contract": {"type": "repository"},
                "plan_first": True,
            },
        )
    ):
        _applied, assignment = optimizer.prepare_task_assignment(
            "excluded_%d" % index, "demo", metadata
        )
        assert assignment is None
    _applied, assignment = optimizer.prepare_task_assignment(
        "almost-certainly-not-sampled",
        "demo",
        {"execution_contract": {"type": "repository"}},
    )
    assert assignment is None


@pytest.mark.parametrize(
    ("decision_status", "experiment_state", "method_name"),
    [
        ("promote", "running", "_promote_experiment"),
        ("reject", "running", "_set_experiment_state"),
        ("rollback", "monitoring", "rollback_policy"),
        ("retain", "monitoring", "_set_experiment_state"),
    ],
)
def test_tick_executes_each_evidence_action(
    monkeypatch, decision_status, experiment_state, method_name
) -> None:
    cp = ControlPlane.in_memory()
    service = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        config=ScientificOptimizerConfig(auto_propose=False),
    )
    experiment = {
        "id": "experiment_tick",
        "project": "demo",
        "state": experiment_state,
        "auto_promote": True,
        "control_policy_id": "policy_control",
        "treatment_policy_id": "policy_treatment",
    }
    calls = []
    monkeypatch.setattr(service, "_claim_tick_lease", lambda: True)
    monkeypatch.setattr(service, "list_experiments", lambda: [experiment])
    monkeypatch.setattr(service, "refresh_experiment", lambda _id: [])
    monkeypatch.setattr(
        service,
        "analyze_experiment",
        lambda _id: {"status": decision_status, "reason": "measured"},
    )
    monkeypatch.setattr(
        service,
        "_promote_experiment",
        lambda *args, **kwargs: calls.append("_promote_experiment"),
    )
    monkeypatch.setattr(
        service,
        "_set_experiment_state",
        lambda *args, **kwargs: calls.append("_set_experiment_state"),
    )
    monkeypatch.setattr(
        service,
        "rollback_policy",
        lambda *args, **kwargs: calls.append("rollback_policy"),
    )
    report = service.tick(trigger="test")
    assert report["status"] == "ok"
    assert method_name in calls


def test_optimizer_thread_lifecycle_and_in_process_busy_guard() -> None:
    cp = ControlPlane.in_memory()
    assert cp.optimizer.start() is False
    service = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        config=ScientificOptimizerConfig(
            enabled=True,
            auto_propose=False,
            initial_delay_seconds=3600,
        ),
    )
    assert service.start() is True
    assert service.start() is False
    assert service.status()["thread_alive"] is True
    assert service.stop() is True
    service._run_lock.acquire()
    try:
        assert service.tick()["status"] == "busy"
    finally:
        service._run_lock.release()


def test_analysis_rejects_guardrail_failure_and_inconclusive_maximum(
    monkeypatch,
) -> None:
    cp, _control, _treatment, experiment = _completed_experiment()
    optimizer = cp.optimizer
    optimizer.refresh_experiment(experiment["id"])
    monkeypatch.setattr(
        optimizer,
        "_compare_metric",
        lambda *args, **kwargs: {
            "metric": "cycles_to_accept",
            "difference": 0.0,
            "ci_lower": -0.1,
            "ci_upper": 0.1,
            "control_mean": 2.0,
            "treatment_mean": 2.0,
        },
    )
    monkeypatch.setattr(
        optimizer,
        "_compare_guardrail",
        lambda *args, **kwargs: {"noninferior": False},
    )
    decision = optimizer.analyze_experiment(experiment["id"])
    assert decision["status"] == "reject"
    assert decision["reason"] == "quality guardrail failed"
    monkeypatch.setattr(
        optimizer,
        "_compare_guardrail",
        lambda *args, **kwargs: {"noninferior": True},
    )
    decision = optimizer.analyze_experiment(experiment["id"])
    assert decision["status"] == "reject"
    assert "maximum sample budget" in decision["reason"]


@pytest.mark.parametrize(
    ("project", "parameters", "baseline", "expected_metric", "expected_parameter"),
    [
        (
            "lower-strength",
            {"model_strength": 6},
            {"cost_known": True, "cost_usd": 1.0, "cycles_to_accept": 1.0},
            "cost_usd",
            ("model_strength", 5),
        ),
        (
            "review-budget",
            {"review_max_iterations": 8},
            {"cost_known": False, "cost_usd": 0.0, "cycles_to_accept": 1.0},
            "total_tokens",
            ("review_max_iterations", 6),
        ),
        (
            "plan-first",
            {},
            {"cost_known": False, "cost_usd": 0.0, "cycles_to_accept": 3.0},
            "cycles_to_accept",
            ("plan_first", True),
        ),
    ],
)
def test_autonomous_policy_hypothesis_branches(
    monkeypatch,
    project,
    parameters,
    baseline,
    expected_metric,
    expected_parameter,
) -> None:
    cp = ControlPlane.in_memory()
    optimizer = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        config=ScientificOptimizerConfig(
            min_baseline_tasks=2,
            default_min_samples_per_arm=2,
            default_max_samples_per_arm=2,
        ),
    )
    policy = optimizer.create_policy("baseline", project, parameters)
    optimizer.promote_policy(policy["id"])
    sample = {
        "task_id": "task_baseline",
        "total_tokens": 100.0,
        **baseline,
    }
    monkeypatch.setattr(
        optimizer, "_project_baseline", lambda _project: [sample, sample]
    )
    experiment = optimizer.propose_next_experiment(project)
    assert experiment["primary_metric"] == expected_metric
    treatment = optimizer.get_policy(experiment["treatment_policy_id"])
    key, value = expected_parameter
    assert treatment["parameters"][key] == value


def test_tick_discovers_projects_and_improvement_early_exits(monkeypatch) -> None:
    cp = ControlPlane.in_memory()
    task = cp.create_task("project signal", project="demo")
    cp.store.execute(
        "UPDATE tasks SET state = 'completed', completed_at = ?, updated_at = ? "
        "WHERE id = ?",
        (utcnow(), utcnow(), task.id),
    )
    service = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        config=ScientificOptimizerConfig(auto_propose=True),
    )
    monkeypatch.setattr(service, "_claim_tick_lease", lambda: True)
    monkeypatch.setattr(service, "list_experiments", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        service,
        "propose_next_experiment",
        lambda project: {"project": project, "status": "proposed"},
    )
    report = service.tick()
    assert report["proposals"] == [{"project": "demo", "status": "proposed"}]
    assert service.propose_improvement_task("demo") is None

    with_callback = ScientificOptimizerService(
        cp.store,
        cp.observability,
        get_task=cp.get_task,
        task_detail=cp.task_detail,
        list_observability=cp.list_observability,
        create_task=cp.create_task,
        config=ScientificOptimizerConfig(min_baseline_tasks=2),
    )
    monkeypatch.setattr(with_callback, "_project_baseline", lambda _project: [])
    assert with_callback.propose_improvement_task("demo") is None
    low_cycle = {
        "task_id": "task_low",
        "cycles_to_accept": 1.0,
        "executor_attempts": 1.0,
        "review_attempts": 1.0,
        "lead_time_ms": 1.0,
        "total_tokens": 1.0,
        "cost_usd": 0.0,
    }
    monkeypatch.setattr(
        with_callback, "_project_baseline", lambda _project: [low_cycle, low_cycle]
    )
    assert with_callback.propose_improvement_task("demo") is None


def test_strength_ladder_readiness_uses_active_selection(monkeypatch) -> None:
    from mac import model_selection

    monkeypatch.setattr(model_selection, "read_active", lambda: {"ladder": ["a", "b"]})
    assert ScientificOptimizerService._strength_ladder_ready() is True
    monkeypatch.setattr(model_selection, "read_active", lambda: None)
    assert ScientificOptimizerService._strength_ladder_ready() is False
