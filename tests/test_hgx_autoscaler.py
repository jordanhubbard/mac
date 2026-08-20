from __future__ import annotations

from datetime import datetime

from mac.hgx_autoscaler import HgxAutoscaler, HgxAutoscalerConfig
from mac.models import ProvisioningStatus, TaskState
from mac.services import ControlPlane


class FakeClock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class FakeController:
    def __init__(self) -> None:
        self.executions: list[dict] = []
        self.retirements: list[dict] = []

    def execute(self, **kwargs):
        self.executions.append(kwargs)
        return {
            "outcome": "attested_capacity_requires_onboarding",
            "created_session_ids": ["session-new"],
        }

    def retire_spare(self, **kwargs):
        self.retirements.append(kwargs)
        return {
            "retired_session_ids": ["session-old"],
            "deletion": {"automatic": True, "performed": True},
        }


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _autoscaler(
    cp: ControlPlane,
    controller: FakeController,
    clock: FakeClock,
    **overrides,
) -> HgxAutoscaler:
    values = {
        "enabled": True,
        "scale_up_stabilization_seconds": 120.0,
        "scale_down_stabilization_seconds": 3600.0,
        "spare_min_age_seconds": 3600.0,
        "scale_up_step": 1,
        "scale_down_step": 1,
    }
    values.update(overrides)
    return HgxAutoscaler(
        cp,
        HgxAutoscalerConfig(**values),
        controller=controller,
        clock=clock,
    )


def test_transient_demand_stabilizes_before_scale_up() -> None:
    cp = ControlPlane.in_memory()
    task = cp.create_task("needs capacity")
    request = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        task_id=task.id,
    )
    clock = FakeClock(_epoch(request.created_at) + 30)
    controller = FakeController()
    autoscaler = _autoscaler(cp, controller, clock)

    report = autoscaler.run_once()

    assert report["action"] == "stabilizing_scale_up"
    assert report["pending_request_count"] == 1
    assert report["sustained_pending_request_count"] == 0
    assert controller.executions == []


def test_sustained_demand_executes_one_bounded_capacity_step() -> None:
    cp = ControlPlane.in_memory()
    task = cp.create_task("needs capacity")
    request = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        task_id=task.id,
    )
    clock = FakeClock(_epoch(request.created_at) + 121)
    controller = FakeController()
    autoscaler = _autoscaler(cp, controller, clock)

    report = autoscaler.run_once()

    assert report["action"] == "scale_up_reconcile"
    assert report["sustained_pending_request_count"] == 1
    assert controller.executions == [
        {"pending_request_count": 1, "registered_agents": None}
    ]


def test_stale_task_request_is_cancelled_before_capacity_math() -> None:
    cp = ControlPlane.in_memory()
    task = cp.create_task("already handled")
    request = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
        task_id=task.id,
    )
    cp.close_task(
        task.id,
        TaskState.CANCELLED.value,
        actor="operator",
        detail={"reason": "not needed"},
    )
    clock = FakeClock(_epoch(request.created_at) + 500)
    controller = FakeController()
    autoscaler = _autoscaler(cp, controller, clock)

    report = autoscaler.run_once()

    assert report["pending_request_count"] == 0
    assert report["reconciled_stale_request_ids"] == [request.id]
    assert cp.provisioning.get_request(request.id).status == (
        ProvisioningStatus.CANCELLED.value
    )
    assert controller.executions == []


def test_taskless_legacy_dispatch_request_is_cancelled() -> None:
    cp = ControlPlane.in_memory()
    request = cp.provisioning.request_agent(
        reason="dispatch.no_eligible_agent",
    )
    clock = FakeClock(_epoch(request.created_at) + 500)
    controller = FakeController()
    autoscaler = _autoscaler(cp, controller, clock)

    report = autoscaler.run_once()

    assert report["pending_request_count"] == 0
    assert report["reconciled_stale_request_ids"] == [request.id]
    assert report["ignored_request_counts"] == {}
    assert cp.provisioning.get_request(request.id).status == (
        ProvisioningStatus.CANCELLED.value
    )
    assert controller.executions == []


def test_reviewer_and_service_role_requests_do_not_create_generic_workers() -> None:
    cp = ControlPlane.in_memory()
    review_task = cp.create_task("needs a reviewer")
    review = cp.provisioning.request_agent(
        reason="review.no_eligible_reviewer",
        task_id=review_task.id,
        capabilities=["review"],
    )
    service = cp.provisioning.request_agent(
        reason="service_role:media:image.generate",
        capabilities=["image_generation"],
    )
    clock = FakeClock(max(_epoch(review.created_at), _epoch(service.created_at)) + 500)
    controller = FakeController()
    autoscaler = _autoscaler(cp, controller, clock)

    report = autoscaler.run_once()

    assert report["pending_request_count"] == 0
    assert report["reconciled_stale_request_ids"] == []
    assert report["ignored_request_counts"] == {
        "review.no_eligible_reviewer": 1,
        "service_role:media:image.generate": 1,
    }
    assert cp.provisioning.get_request(review.id).status == (
        ProvisioningStatus.PENDING.value
    )
    assert cp.provisioning.get_request(service.id).status == (
        ProvisioningStatus.PENDING.value
    )
    assert controller.executions == []


def test_scale_down_waits_for_sustained_zero_demand_then_retires_one_spare() -> None:
    cp = ControlPlane.in_memory()
    clock = FakeClock(1_700_000_000.0)
    controller = FakeController()
    autoscaler = _autoscaler(cp, controller, clock)

    early = autoscaler.run_once()
    clock.now += 3601
    late = autoscaler.run_once()

    assert early["action"] == "stabilizing_scale_down"
    assert controller.retirements == [
        {
            "pending_request_count": 0,
            "min_age_seconds": 3600.0,
            "max_delete_count": 1,
            "registered_agents": None,
        }
    ]
    assert late["action"] == "scale_down_reconcile"


def test_config_from_env_builds_bounded_step_policy() -> None:
    config = HgxAutoscalerConfig.from_env(
        {
            "MAC_HGX_AUTOSCALE_ENABLED": "1",
            "MAC_HGX_AUTOSCALE_MAX_SESSIONS": "7",
            "MAC_HGX_AUTOSCALE_SCALE_UP_STEP": "2",
            "MAC_HGX_AUTOSCALE_SCALE_DOWN_STEP": "1",
            "MAC_HGX_AUTOSCALE_SCALE_UP_STABILIZATION_SECONDS": "180",
        }
    )

    assert config.active is True
    assert config.max_sessions == 7
    assert config.scale_up_stabilization_seconds == 180
    assert config.capacity_policy().max_create_per_run == 2


def test_config_from_env_passes_capability_args_to_the_capacity_policy() -> None:
    config = HgxAutoscalerConfig.from_env(
        {
            "MAC_HGX_AUTOSCALE_ENABLED": "1",
            "MAC_HGX_AUTOSCALE_CREATE_EXTRA_ARGS": "--cap-add NET_ADMIN",
        }
    )

    assert config.active is True
    assert config.capacity_policy().create_extra_args == ("--cap-add", "NET_ADMIN")


def test_config_from_env_reports_invalid_capability_args_instead_of_creating() -> None:
    config = HgxAutoscalerConfig.from_env(
        {
            "MAC_HGX_AUTOSCALE_ENABLED": "1",
            "MAC_HGX_AUTOSCALE_CREATE_EXTRA_ARGS": "--name=$(whoami)",
        }
    )

    assert config.active is False
    assert "create_extra_args" in config.configuration_error
