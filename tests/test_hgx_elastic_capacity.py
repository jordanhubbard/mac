from __future__ import annotations

import fcntl
import json
import os
import subprocess
from pathlib import Path

import pytest

from mac.cli import build_parser
from mac.hgx_elastic_capacity import (
    CAPACITY_SCHEMA,
    HgxCapacityError,
    HgxCapacityPolicy,
    HgxElasticCapacityController,
    count_pending_provisioning_requests,
    normalize_registered_fungible_agents,
)
from mac.hgx_provider import (
    STANDARD_DIND_FLAVOR,
    HgxCommandError,
    HgxError,
    HgxProvider,
    HgxSession,
)
from mac.models import ValidationError


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeProvider:
    def __init__(
        self,
        sessions: list[HgxSession] | None = None,
        *,
        attest_after: int = 1,
        create_error: HgxError | None = None,
    ) -> None:
        self.sessions = list(sessions or [])
        self.attest_after = attest_after
        self.create_error = create_error
        self.created: list[tuple[str, str, list[str]]] = []
        self.status_ids: list[str] = []
        self.attest_ids: list[str] = []
        self.attest_attempts: dict[str, int] = {}
        self.deleted_ids: list[str] = []

    def list(self) -> list[HgxSession]:
        return list(self.sessions)

    def status(self, session_id: str) -> HgxSession:
        self.status_ids.append(session_id)
        return next(item for item in self.sessions if item.session_id == session_id)

    def create_standard_dind(
        self,
        *,
        name: str | None = None,
        extra_args: list[str] | None = None,
    ) -> HgxSession:
        if self.create_error is not None:
            raise self.create_error
        session_id = "sess-%d" % (len(self.created) + 1)
        session = HgxSession(
            session_id=session_id,
            name=name or "",
            flavor=STANDARD_DIND_FLAVOR,
            state="running",
        )
        self.created.append(
            (STANDARD_DIND_FLAVOR, name or "", list(extra_args or []))
        )
        self.sessions.append(session)
        return session

    def attest_ssh(self, session_id: str) -> str:
        self.attest_ids.append(session_id)
        attempts = self.attest_attempts.get(session_id, 0) + 1
        self.attest_attempts[session_id] = attempts
        if attempts < self.attest_after:
            raise HgxError("not reachable yet")
        return session_id

    def delete(self, session_id: str) -> str:
        self.deleted_ids.append(session_id)
        self.sessions = [
            session for session in self.sessions if session.session_id != session_id
        ]
        return session_id


def _controller(
    tmp_path: Path,
    provider: FakeProvider,
    clock: FakeClock,
    *,
    policy: HgxCapacityPolicy,
) -> HgxElasticCapacityController:
    return HgxElasticCapacityController(
        provider=provider,
        policy=policy,
        state_path=tmp_path / "capacity.json",
        clock=clock,
        sleeper=clock.sleep,
    )


def test_policy_is_bounded_and_pending_requests_drive_headroom() -> None:
    policy = HgxCapacityPolicy(min_ready=1, max_sessions=4, headroom=1)

    assert policy.desired_ready(0) == 1
    assert policy.desired_ready(2) == 3
    assert policy.desired_ready(10) == 4
    with pytest.raises(ValidationError):
        HgxCapacityPolicy(min_ready=2, max_sessions=1)
    with pytest.raises(ValidationError):
        HgxCapacityPolicy(gpu_count=9)
    with pytest.raises(ValidationError):
        HgxCapacityPolicy(memory_gib=512)
    with pytest.raises(ValidationError):
        HgxCapacityPolicy(cluster="gke newhouse")
    with pytest.raises(ValidationError):
        HgxCapacityPolicy(max_create_per_run=0)


def test_count_pending_provisioning_requests_deduplicates_durable_ids() -> None:
    requests = [
        {"id": "prov-1", "status": "pending"},
        {"id": "prov-1", "status": "pending"},
        {"id": "prov-2", "status": "fulfilled"},
        {"id": "prov-3", "status": "pending"},
    ]

    assert count_pending_provisioning_requests(requests) == 2


def test_plan_and_status_are_read_only_and_prefer_attesting_existing(
    tmp_path: Path,
) -> None:
    session = HgxSession(
        session_id="immutable-a",
        name="display-only",
        flavor=STANDARD_DIND_FLAVOR,
        state="running",
    )
    provider = FakeProvider([session])
    clock = FakeClock()
    state_path = tmp_path / "capacity.json"
    state_path.write_text(
        json.dumps(
            {
                "schema": CAPACITY_SCHEMA,
                "sessions": {
                    "immutable-a": {
                        "session_id": "immutable-a",
                        "created_by_controller": True,
                        "attestation_status": "pending",
                        "onboarding_status": "not_onboarded",
                    }
                },
                "last_create_at": None,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    original_state = state_path.read_bytes()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(min_ready=1, max_sessions=3),
    )

    plan = controller.plan()
    status = controller.status()

    assert plan["read_only"] is True
    assert plan["actions"] == [
        {
            "action": "attest_existing",
            "session_ids": ["immutable-a"],
            "requires_execute": True,
        }
    ]
    assert status["read_only"] is True
    assert provider.created == []
    assert provider.status_ids == []
    assert provider.attest_ids == []
    assert state_path.read_bytes() == original_state


def test_untracked_busy_dind_sessions_consume_quota_but_not_pending_supply(
    tmp_path: Path,
) -> None:
    sessions = [
        HgxSession(
            session_id="busy-%d" % index,
            name="worker-%d" % index,
            flavor=STANDARD_DIND_FLAVOR,
            state="running",
        )
        for index in range(5)
    ]
    provider = FakeProvider(sessions)
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(max_sessions=6),
    )

    plan = controller.plan(pending_request_count=1)

    assert plan["live_provider_session_count"] == 5
    assert plan["available_capacity_session_count"] == 0
    assert plan["untracked_live_session_ids"] == [
        "busy-0",
        "busy-1",
        "busy-2",
        "busy-3",
        "busy-4",
    ]
    assert plan["known_attested_session_ids"] == []
    assert plan["create_count"] == 1
    assert plan["actions"] == [
        {
            "action": "create_standard_dind",
            "count": 1,
            "requires_execute": True,
        }
    ]
    assert provider.status_ids == []
    assert provider.attest_ids == []


def test_execute_surfaces_provider_429_without_reusing_busy_workers(
    tmp_path: Path,
) -> None:
    sessions = [
        HgxSession(
            session_id="busy-%d" % index,
            flavor=STANDARD_DIND_FLAVOR,
            state="running",
        )
        for index in range(5)
    ]
    provider = FakeProvider(
        sessions,
        create_error=HgxCommandError(
            "hgx create failed",
            argv=["admin", "hgx", "--json", "create"],
            returncode=1,
            stderr="HTTP 429 quota exceeded secret-detail-must-not-leak",
        ),
    )
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(max_sessions=6),
    )

    result = controller.execute(pending_request_count=1)

    assert result["outcome"] == "provider_quota_exhausted"
    assert result["provider_create_failure_class"] == "provider_quota_exhausted"
    assert result["ready_gap"] == 1
    assert provider.status_ids == []
    assert provider.attest_ids == []
    assert result["next_actions"] == [
        {
            "action": "wait_for_provider_quota_or_raise_bound",
            "ready_gap": 1,
            "failure_class": "provider_quota_exhausted",
        }
    ]
    assert "secret-detail-must-not-leak" not in json.dumps(result)


def test_execute_creates_standard_dind_waits_for_nonce_attestation_and_persists(
    tmp_path: Path,
) -> None:
    provider = FakeProvider(attest_after=2)
    clock = FakeClock()
    state_path = tmp_path / "capacity.json"
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(
            min_ready=1,
            max_sessions=2,
            wait_timeout_seconds=10,
            poll_interval_seconds=1,
        ),
    )

    result = controller.execute()

    assert result["outcome"] == "attested_capacity_requires_onboarding"
    assert result["created_session_ids"] == ["sess-1"]
    assert result["attested_session_ids"] == ["sess-1"]
    assert provider.created[0][0] == STANDARD_DIND_FLAVOR
    assert provider.created[0][2] == [
        "--cluster",
        "gke-newhouse",
        "--gpu",
        "1",
        "--memory",
        "64Gi",
        "--cpu",
        "8",
    ]
    assert provider.status_ids == ["sess-1", "sess-1"]
    assert provider.attest_ids == ["sess-1", "sess-1"]
    assert result["deletion"] == {"automatic": False, "performed": False}
    action = result["next_actions"][0]
    assert action["action"] == "prepare_fungible_onboarding"
    assert action["session_id"] == "sess-1"
    assert action["automatic_fulfillment"] is False

    persisted = json.loads(state_path.read_text(encoding="utf-8"))
    assert persisted["schema"] == CAPACITY_SCHEMA
    assert persisted["sessions"]["sess-1"]["attestation_status"] == "passed"
    assert persisted["sessions"]["sess-1"]["next_action"]["action"] == (
        "prepare_fungible_onboarding"
    )
    assert state_path.stat().st_mode & 0o777 == 0o600


def test_execute_passes_exact_current_hgx_create_argv(
    tmp_path: Path, monkeypatch
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        command = list(argv)
        calls.append(command)
        if command == ["admin", "hgx", "--json", "list"]:
            stdout = "[]"
        elif command[:3] == ["admin", "hgx", "--json", "create"]:
            stdout = json.dumps(
                {
                    "id": "immutable-new",
                    "agent_type": STANDARD_DIND_FLAVOR,
                    "status": "running",
                }
            )
        elif command[:3] == ["admin", "hgx", "--json", "status"]:
            stdout = json.dumps(
                {
                    "id": "immutable-new",
                    "agent_type": STANDARD_DIND_FLAVOR,
                    "status": "running",
                }
            )
        elif command[:2] == ["admin", "hgx", "ssh"]:
            stdout = command[-1] + "\n"
        else:
            raise AssertionError("unexpected hgx argv: %r" % command)
        return subprocess.CompletedProcess(command, 0, stdout, "")

    monkeypatch.setattr("mac.hgx_provider.subprocess.run", fake_run)
    clock = FakeClock()
    controller = HgxElasticCapacityController(
        provider=HgxProvider(binary="hgx"),
        policy=HgxCapacityPolicy(max_sessions=1),
        state_path=tmp_path / "capacity.json",
        clock=clock,
        sleeper=clock.sleep,
    )

    result = controller.execute(pending_request_count=1)

    assert result["attested_session_ids"] == ["immutable-new"]
    assert calls[1] == [
        "admin", "hgx",
        "--json",
        "create",
        "--type",
        "standard-dind",
        "--name",
        "mac-fungible-20231114-221320-01",
        "--cluster",
        "gke-newhouse",
        "--gpu",
        "1",
        "--memory",
        "64Gi",
        "--cpu",
        "8",
    ]


def test_create_exit_alone_never_counts_as_ready(tmp_path: Path) -> None:
    provider = FakeProvider(attest_after=100)
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(
            min_ready=1,
            max_sessions=1,
            wait_timeout_seconds=2,
            poll_interval_seconds=1,
        ),
    )

    result = controller.execute()

    assert result["created_session_ids"] == ["sess-1"]
    assert result["attested_session_ids"] == []
    assert result["ready_gap"] == 1
    assert result["outcome"] == "capacity_bound_reached"
    assert result["next_actions"][0] == {
        "action": "retry_or_retire_explicitly",
        "session_id": "sess-1",
        "automatic_deletion": False,
    }
    assert result["next_actions"][1]["action"] == (
        "review_failed_sessions_or_capacity_bound"
    )
    persisted = json.loads((tmp_path / "capacity.json").read_text())
    assert persisted["sessions"]["sess-1"]["attestation_status"] == "failed"
    assert persisted["sessions"]["sess-1"]["failure_class"] == (
        "ssh_attestation_timeout"
    )


def test_cooldown_blocks_a_second_scale_up_without_deleting(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(
            min_ready=1,
            max_sessions=3,
            cooldown_seconds=60,
        ),
    )
    first = controller.execute()
    assert first["created_session_ids"] == ["sess-1"]

    plan = controller.plan(pending_request_count=2)

    assert plan["ready_gap"] == 1
    assert plan["create_count"] == 0
    assert plan["cooldown_remaining_seconds"] == 60
    assert plan["deletion"]["automatic"] is False
    assert len(provider.created) == 1


def test_execute_ramps_up_one_session_per_run_even_for_large_backlog(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(
            max_sessions=10,
            max_create_per_run=1,
            cooldown_seconds=0,
        ),
    )

    first = controller.execute(pending_request_count=8)
    second = controller.execute(pending_request_count=8)

    assert first["created_session_ids"] == ["sess-1"]
    assert first["ready_gap"] == 7
    assert second["created_session_ids"] == ["sess-2"]
    assert second["ready_gap"] == 6
    assert len(provider.created) == 2


def test_retire_spare_deletes_only_old_unonboarded_surplus(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(
            max_sessions=4,
            max_create_per_run=2,
            cooldown_seconds=0,
        ),
    )
    created = controller.execute(pending_request_count=2)
    assert created["created_session_ids"] == ["sess-1", "sess-2"]
    controller.mark_onboarded("sess-1", agent_id="agent_worker_1")
    clock.now += 7200

    result = controller.retire_spare(
        pending_request_count=0,
        min_age_seconds=3600,
        max_delete_count=1,
    )

    assert result["retired_session_ids"] == ["sess-2"]
    assert result["protected_onboarded_session_ids"] == ["sess-1"]
    assert provider.deleted_ids == ["sess-2"]
    persisted = json.loads((tmp_path / "capacity.json").read_text())
    assert persisted["sessions"]["sess-2"]["retirement_status"] == "retired"
    assert persisted["sessions"]["sess-1"]["onboarding_status"] == "onboarded"


def test_retire_spare_preserves_registered_and_young_sessions(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(
            max_sessions=4,
            max_create_per_run=2,
            cooldown_seconds=0,
        ),
    )
    controller.execute(pending_request_count=2)
    controller.mark_onboarded("sess-1", agent_id="agent_worker_1")

    result = controller.retire_spare(
        pending_request_count=0,
        min_age_seconds=3600,
        max_delete_count=2,
        registered_agents={"sess-1": "agent_worker_1"},
    )

    assert result["retired_session_ids"] == []
    assert result["protected_onboarded_session_ids"] == ["sess-1"]
    assert provider.deleted_ids == []


def test_mark_onboarded_consumes_supply_and_new_pending_demand_creates_again(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(
            max_sessions=2,
            cooldown_seconds=0,
        ),
    )
    first = controller.execute(pending_request_count=1)
    assert first["created_session_ids"] == ["sess-1"]

    consumed = controller.mark_onboarded("sess-1", agent_id="agent_worker_1")
    assert consumed["available_for_pending_supply"] is False
    assert consumed["provider_mutation"] is False
    assert consumed["idempotent"] is False
    assert controller.mark_onboarded(
        "sess-1", agent_id="agent_worker_1"
    )["idempotent"] is True

    plan = controller.plan(pending_request_count=1)
    assert plan["onboarded_session_ids"] == ["sess-1"]
    assert plan["known_attested_session_ids"] == []
    assert plan["create_count"] == 1

    second = controller.execute(pending_request_count=1)
    assert second["created_session_ids"] == ["sess-2"]
    assert second["attested_session_ids"] == ["sess-2"]
    assert provider.status_ids == ["sess-1", "sess-2"]
    with pytest.raises(HgxCapacityError, match="already consumed"):
        controller.mark_onboarded("sess-1", agent_id="agent_other")


def test_onboarded_supply_survives_provider_dropping_the_live_session(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(
            max_sessions=2,
            cooldown_seconds=0,
        ),
    )
    controller.execute(pending_request_count=1)
    controller.mark_onboarded("sess-1", agent_id="agent_worker_1")

    # The provider drops/terminates the onboarded session so it no longer
    # appears in the live inventory, but the durable receipt still records it
    # as onboarded supply.
    provider.sessions = []

    plan = controller.plan(pending_request_count=1)
    assert plan["onboarded_session_ids"] == ["sess-1"]
    assert plan["live_provider_session_count"] == 0

    status = controller.status(pending_request_count=1)
    assert status["onboarded_session_ids"] == ["sess-1"]


def test_execute_fails_fast_when_another_capacity_mutation_holds_the_lock(
    tmp_path: Path,
) -> None:
    provider = FakeProvider()
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(min_ready=1, max_sessions=1),
    )
    lock_path = tmp_path / "capacity.json.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        with pytest.raises(HgxCapacityError, match="already active"):
            controller.execute()
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)

    assert provider.created == []


def test_cli_registers_explicit_execute_separately_from_read_only_commands() -> None:
    parser = build_parser()

    plan = parser.parse_args(["admin", "hgx", "capacity", "plan"])
    status = parser.parse_args(["admin", "hgx", "capacity", "status"])
    execute = parser.parse_args(
        [
            "admin", "hgx",
            "capacity",
            "execute",
            "--pending-requests",
            "2",
            "--max-sessions",
            "4",
        ]
    )
    mark_onboarded = parser.parse_args(
        [
            "admin", "hgx",
            "capacity",
            "mark-onboarded",
            "session-immutable",
            "--agent-id",
            "agent-real",
        ]
    )

    assert plan.func.__name__ == "cmd_hgx_capacity_plan"
    assert status.func.__name__ == "cmd_hgx_capacity_status"
    assert execute.func.__name__ == "cmd_hgx_capacity_execute"
    assert execute.pending_requests == 2
    assert execute.max_sessions == 4
    assert execute.cluster == "gke-newhouse"
    assert execute.gpu == 1
    assert execute.memory_gib == 64
    assert execute.cpu == 8
    assert mark_onboarded.func.__name__ == "cmd_hgx_capacity_mark_onboarded"
    assert mark_onboarded.session_id == "session-immutable"
    assert mark_onboarded.agent_id == "agent-real"


def _running_dind(session_id: str, name: str = "") -> HgxSession:
    return HgxSession(
        session_id=session_id,
        name=name or session_id,
        flavor=STANDARD_DIND_FLAVOR,
        state="running",
    )


def test_normalize_registered_fungible_agents_accepts_maps_and_records() -> None:
    assert normalize_registered_fungible_agents(None) == {}
    assert normalize_registered_fungible_agents(
        {"sess-a": "agent_a", "sess-b": " "}
    ) == {"sess-a": "agent_a"}
    assert normalize_registered_fungible_agents(
        [
            {"session_id": "sess-a", "agent_id": "agent_a"},
            ("sess-b", "agent_b"),
            {"hgx_session_id": "sess-c", "agent": "agent_c"},
            {"session_id": "", "agent_id": "agent_x"},
        ]
    ) == {"sess-a": "agent_a", "sess-b": "agent_b", "sess-c": "agent_c"}
    with pytest.raises(ValidationError):
        normalize_registered_fungible_agents("sess-a")
    with pytest.raises(ValidationError, match="two registered agents"):
        normalize_registered_fungible_agents(
            [("sess-a", "agent_a"), ("sess-a", "agent_b")]
        )


def test_onboarded_sessions_are_reconciled_by_identity_and_block_creation(
    tmp_path: Path,
) -> None:
    # The exact live-capacity incident: five running, already-onboarded DIND
    # sessions with no local receipts. Matching immutable identity to the fleet
    # registry counts them as healthy supply instead of untracked quota.
    sessions = [_running_dind("hgx-%d" % index) for index in range(5)]
    registered = {
        "hgx-%d" % index: "agent_worker_%d" % index for index in range(5)
    }
    provider = FakeProvider(sessions)
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(max_sessions=6),
    )

    plan = controller.plan(pending_request_count=1, registered_agents=registered)

    assert plan["onboarded_session_ids"] == sorted(registered)
    assert plan["untracked_live_session_ids"] == []
    assert plan["reconciled_onboarded_session_ids"] == sorted(registered)
    assert plan["create_count"] == 0
    assert plan["actions"] == []
    assert provider.status_ids == []
    assert provider.attest_ids == []
    # plan/status stay read-only: no receipt is persisted.
    assert not (tmp_path / "capacity.json").exists()


def test_execute_no_op_when_registry_already_satisfies_demand(
    tmp_path: Path,
) -> None:
    sessions = [_running_dind("hgx-%d" % index) for index in range(5)]
    registered = {
        "hgx-%d" % index: "agent_worker_%d" % index for index in range(5)
    }
    provider = FakeProvider(
        sessions,
        create_error=HgxCommandError(
            "hgx create failed",
            argv=["admin", "hgx", "--json", "create"],
            returncode=1,
            stderr="HTTP 429 resource exhausted",
        ),
    )
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(max_sessions=6),
    )

    result = controller.execute(
        pending_request_count=1, registered_agents=registered
    )

    assert result["outcome"] == "capacity_satisfied"
    assert result["created_session_ids"] == []
    assert result["provider_create_failure_class"] is None
    assert result["ready_gap"] == 0
    assert result["reconciled_onboarded_session_ids"] == sorted(registered)
    # No create was attempted, so the provider quota is never touched.
    assert provider.created == []
    assert provider.status_ids == []
    persisted = json.loads((tmp_path / "capacity.json").read_text())
    record = persisted["sessions"]["hgx-0"]
    assert record["onboarding_status"] == "onboarded"
    assert record["onboarded_agent_id"] == "agent_worker_0"
    assert record["reconciled_from_registry"] is True


def test_replacement_session_reconciles_new_identity_and_ignores_old(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([_running_dind("hgx-old")])
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(max_sessions=2, cooldown_seconds=0),
    )

    first = controller.execute(
        pending_request_count=1,
        registered_agents={"hgx-old": "agent_worker_1"},
    )
    assert first["reconciled_onboarded_session_ids"] == ["hgx-old"]
    assert first["outcome"] == "capacity_satisfied"
    assert provider.created == []

    # The provider recreates the worker under a fresh immutable session ID; the
    # fleet registry now binds the same agent slot to the replacement session.
    provider.sessions = [_running_dind("hgx-new")]

    plan = controller.plan(
        pending_request_count=1,
        registered_agents={"hgx-new": "agent_worker_1"},
    )
    # Only the live replacement session counts as healthy onboarded capacity;
    # the stale hgx-old receipt lingers durably but is no longer live supply.
    assert plan["registry_onboarded_session_ids"] == ["hgx-new"]
    assert "hgx-new" in plan["reconciled_onboarded_session_ids"]
    assert "hgx-old" not in plan["registry_onboarded_session_ids"]
    assert plan["untracked_live_session_ids"] == []
    assert plan["create_count"] == 0


def test_terminated_registered_session_is_not_counted_as_healthy_supply(
    tmp_path: Path,
) -> None:
    dead = HgxSession(
        session_id="hgx-dead",
        name="worker",
        flavor=STANDARD_DIND_FLAVOR,
        state="terminated",
    )
    provider = FakeProvider([dead])
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(max_sessions=2, cooldown_seconds=0),
    )

    result = controller.execute(
        pending_request_count=1,
        registered_agents={"hgx-dead": "agent_worker_1"},
    )

    # A terminated session is not reconciled as healthy onboarded capacity, so
    # the controller still creates the missing supply within its bounds.
    assert result["reconciled_onboarded_session_ids"] == []
    assert result["created_session_ids"] == ["sess-1"]
    assert result["attested_session_ids"] == ["sess-1"]
    persisted = json.loads((tmp_path / "capacity.json").read_text())
    assert "hgx-dead" not in persisted.get("sessions", {}) or (
        persisted["sessions"]["hgx-dead"].get("onboarding_status")
        != "onboarded"
    )


def test_registered_agents_without_matching_live_session_are_ignored(
    tmp_path: Path,
) -> None:
    provider = FakeProvider([])
    clock = FakeClock()
    controller = _controller(
        tmp_path,
        provider,
        clock,
        policy=HgxCapacityPolicy(max_sessions=2, cooldown_seconds=0),
    )

    result = controller.execute(
        pending_request_count=1,
        registered_agents={"hgx-ghost": "agent_worker_1"},
    )

    assert result["reconciled_onboarded_session_ids"] == []
    assert result["created_session_ids"] == ["sess-1"]


def test_cli_accepts_registered_agents_file_for_capacity_commands() -> None:
    parser = build_parser()

    plan = parser.parse_args(
        [
            "admin", "hgx",
            "capacity",
            "plan",
            "--registered-agents-file",
            "/tmp/registered.json",
        ]
    )
    execute = parser.parse_args(
        [
            "admin", "hgx",
            "capacity",
            "execute",
            "--registered-agents-file",
            "/tmp/registered.json",
        ]
    )

    assert plan.registered_agents_file == "/tmp/registered.json"
    assert execute.registered_agents_file == "/tmp/registered.json"
    assert parser.parse_args(
        ["admin", "hgx", "capacity", "status"]
    ).registered_agents_file is None
