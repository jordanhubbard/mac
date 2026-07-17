from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any

from mac.store import SQLiteStore
from mac.work_package_models import WORK_PACKAGE_PLAN_SCHEMA
from mac.work_package_pipeline import (
    PipelineReleaseGate,
    PipelineSnapshot,
    ServicePipelineInventory,
    WorkPackagePipelineConfig,
    WorkPackagePipelineController,
    control_plane_pipeline_observer,
)
from mac.work_package_service import (
    RepositoryBaseAttestation,
    WorkPackageService,
)


class _Inventory:
    def __init__(self, runs: list[list[PipelineSnapshot]]) -> None:
        self.runs = list(runs)
        self.calls: list[tuple[str, int]] = []

    def discover(self, *, after_key: str, limit: int) -> list[PipelineSnapshot]:
        self.calls.append((after_key, limit))
        return self.runs.pop(0) if self.runs else []


class _CatalogVerifier:
    def verify(self, repository, *, planning_base_ref, planning_base_sha):
        return RepositoryBaseAttestation(
            repository_id=repository["id"],
            planning_base_ref=planning_base_ref,
            planning_base_sha=planning_base_sha,
            canonical_ref_sha=planning_base_sha,
            source_kind="test",
            verified_at="attested",
            resource_namespace={"status": "unresolved"},
        )


class _Gates:
    def __init__(self, values: dict[str, PipelineReleaseGate] | None = None) -> None:
        self.values = values or {}
        self.calls: list[str] = []

    def resolve(self, snapshot: PipelineSnapshot) -> PipelineReleaseGate:
        self.calls.append(snapshot.key)
        return self.values.get(
            snapshot.key,
            PipelineReleaseGate(True, True, endpoint="endpoint"),
        )


class _Bundles:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.calls: list[str] = []

    def ensure_bundle(self, snapshot: PipelineSnapshot) -> Path:
        self.calls.append(snapshot.key)
        return self.path


class _Integration:
    def __init__(self, calls: list[str], *, fail_key: str = "") -> None:
        self.calls = calls
        self.fail_key = fail_key

    def create_batch(self, package_id: str, node_key: str, *, actor: str) -> dict:
        self.calls.append("create:%s:%s" % (package_id, node_key))
        if package_id == self.fail_key:
            raise RuntimeError(
                "push https://user:secret@example.invalid failed; "
                "Authorization=Bearer abcdef"
            )
        return {"status": "queued", "created": True}

    def assemble(self, batch_id: str) -> dict:
        self.calls.append("assemble:%s" % batch_id)
        return {"status": "assembled", "recovered": False}


class _Certification:
    def __init__(self, calls: list[str], *, run_status: str = "passed") -> None:
        self.calls = calls
        self.run_status = run_status

    def prepare(self, batch_id: str, bundle_path: Path, *, actor: str) -> dict:
        self.calls.append("prepare:%s" % batch_id)
        return {"id": "job_prepared", "status": "queued", "created": True}

    def run(
        self,
        job_id: str,
        bundle_path: Path,
        *,
        owner: str | None = None,
    ) -> dict:
        self.calls.append("certify:%s" % job_id)
        return {
            "status": self.run_status,
            "certification_id": "cert" if self.run_status == "failed" else "",
            "created": True,
        }


class _Landing:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def accept_certification(
        self,
        batch_id: str,
        endpoint: Any,
        *,
        certification_id: str,
    ) -> dict:
        self.calls.append("accept:%s:%s" % (batch_id, certification_id))
        return {"status": "certified"}

    def land(self, batch_id: str, endpoint: Any) -> dict:
        self.calls.append("land:%s" % batch_id)
        return {"status": "landed"}


class _Finalization:
    def __init__(self, calls: list[str], *, error: Exception | None = None) -> None:
        self.calls = calls
        self.error = error

    def finalize_landed_batch(self, batch_id: str, *, actor: str) -> dict:
        self.calls.append("finalize:%s" % batch_id)
        if self.error is not None:
            raise self.error
        return {
            "status": "completed",
            "batch_state": "published",
            "landing_receipt_id": "receipt",
            "provenance_verified": True,
            "integration_task_id": "task_assemble",
            "certification_task_id": "task_certify",
            "integration_node_state": "integrated",
            "certification_node_state": "certified",
            "integration_task_completed": True,
            "held_wip_count": 0,
            "package_state": "completed",
            "created": True,
        }


class _Rejection:
    def __init__(
        self,
        calls: list[str],
        *,
        complete_receipt: bool = True,
    ) -> None:
        self.calls = calls
        self.complete_receipt = complete_receipt

    def reject_failed_certification(
        self,
        batch_id: str,
        *,
        certification_id: str,
        actor: str,
    ) -> dict:
        self.calls.append("reject:%s:%s" % (batch_id, certification_id))
        if not self.complete_receipt:
            return {"status": "rejected"}
        return {
            "status": "completed",
            "batch_state": "rejected",
            "certification_id": certification_id,
            "provenance_verified": True,
            "integration_task_id": "task_assemble",
            "certification_task_id": "task_certify",
            "integration_node_state": "integrated",
            "certification_node_state": "rejected",
            "andon_recorded": True,
            "package_state": "paused",
            "wip_disposition": "quarantined",
            "held_wip_count": 0,
            "created": True,
        }


def _snapshot(
    *,
    key: str = "wp:1:1:assemble",
    package: str = "wp",
    batch: str = "",
    batch_state: str = "",
    job: str = "",
    job_state: str = "",
    certification: str = "",
    product_finalized: bool = False,
    integration_state: str | None = None,
    certification_state: str | None = None,
) -> PipelineSnapshot:
    if integration_state is None:
        integration_state = (
            "integrated"
            if batch_state in {"verifying", "certified", "rejected", "published"}
            else "ready"
        )
    if certification_state is None:
        if batch_state in {"certified", "published"} or job_state == "completed":
            certification_state = "certified"
        elif job_state == "failed":
            certification_state = "rejected"
        elif batch_state == "verifying":
            certification_state = "ready"
        else:
            certification_state = "planned"
    return PipelineSnapshot(
        key=key,
        package_id=package,
        plan_version=1,
        epoch=1,
        integration_node_key="assemble",
        integration_task_id="task_assemble",
        integration_node_state=integration_state,
        certification_node_key="certify",
        certification_task_id="task_certify",
        certification_node_state=certification_state,
        batch_id=batch,
        batch_state=batch_state,
        certification_job_id=job,
        certification_job_state=job_state,
        certification_id=certification,
        product_finalized=product_finalized,
    )


def _controller(
    tmp_path: Path,
    inventory: _Inventory,
    *,
    gates: _Gates | None = None,
    integration: _Integration | None = None,
    certification: _Certification | None = None,
    finalization: _Finalization | None = None,
    rejection: _Rejection | None = None,
    observer: Any = None,
    max_actions: int = 8,
    max_items: int = 16,
    enabled: bool = False,
    initial_delay: float = 5.0,
) -> tuple[WorkPackagePipelineController, list[str]]:
    bundle = tmp_path / "candidate.bundle"
    bundle.write_bytes(b"exact bundle")
    calls: list[str] = []
    return (
        WorkPackagePipelineController(
            inventory=inventory,
            release_gates=gates or _Gates(),
            bundles=_Bundles(bundle),
            integration=integration or _Integration(calls),
            certification=certification or _Certification(calls),
            landing=_Landing(calls),
            finalization=finalization or _Finalization(calls),
            rejection=rejection or _Rejection(calls),
            config=WorkPackagePipelineConfig(
                enabled=enabled,
                interval_seconds=60,
                initial_delay_seconds=initial_delay,
                max_actions_per_run=max_actions,
                max_items_per_run=max_items,
            ),
            observer=observer,
            owner="pipeline-test",
        ),
        calls,
    )


def test_pipeline_advances_one_durable_station_per_item_per_pass(
    tmp_path: Path,
) -> None:
    inventory = _Inventory(
        [
            [_snapshot()],
            [_snapshot(batch="batch", batch_state="queued")],
            [_snapshot(batch="batch", batch_state="verifying")],
            [
                _snapshot(
                    batch="batch",
                    batch_state="verifying",
                    job="job",
                    job_state="queued",
                )
            ],
            [
                _snapshot(
                    batch="batch",
                    batch_state="verifying",
                    job="job",
                    job_state="completed",
                    certification="cert",
                )
            ],
            [_snapshot(batch="batch", batch_state="certified")],
            [_snapshot(batch="batch", batch_state="published")],
        ]
    )
    controller, calls = _controller(tmp_path, inventory)

    reports = [controller.run_once() for _ in range(7)]

    assert calls == [
        "create:wp:assemble",
        "assemble:batch",
        "prepare:batch",
        "certify:job",
        "accept:batch:cert",
        "land:batch",
        "finalize:batch",
    ]
    assert all(report.action_count == 1 for report in reports)
    assert [report.outcomes[0].station for report in reports] == [
        "integration_batch",
        "integration_assembly",
        "certification_prepare",
        "certification_run",
        "certification_acceptance",
        "landing",
        "product_finalization",
    ]
    assert reports[2].outcomes[0].job_id == "job_prepared"


def test_downstream_release_gate_blocks_upstream_wip_but_not_independent_peer(
    tmp_path: Path,
) -> None:
    blocked = _snapshot(key="a", package="blocked")
    ready = _snapshot(key="b", package="ready")
    gates = _Gates(
        {
            "a": PipelineReleaseGate(
                False,
                True,
                endpoint="endpoint",
                reason="repository has no certification contract",
            )
        }
    )
    inventory = _Inventory([[blocked, ready]])
    controller, calls = _controller(
        tmp_path,
        inventory,
        gates=gates,
        max_actions=1,
    )

    report = controller.run_once()

    assert calls == ["create:ready:assemble"]
    assert report.scanned_count == 2
    assert report.action_count == 1
    assert report.outcomes[0].code == "certification_contract_unavailable"
    assert report.outcomes[0].attempted is False
    assert report.outcomes[1].status == "advanced"


def test_landing_disabled_is_a_fail_closed_hold(tmp_path: Path) -> None:
    snapshot = _snapshot(batch="batch", batch_state="queued")
    gates = _Gates(
        {
            snapshot.key: PipelineReleaseGate(
                True,
                False,
                endpoint="endpoint",
                reason="automatic landing disabled",
            )
        }
    )
    controller, calls = _controller(
        tmp_path,
        _Inventory([[snapshot]]),
        gates=gates,
    )

    report = controller.run_once()

    assert calls == []
    assert report.status == "blocked"
    assert report.outcomes[0].code == "landing_disabled"


def test_action_budget_is_hard_and_cursor_moves_only_over_scanned_items(
    tmp_path: Path,
) -> None:
    snapshots = [
        _snapshot(key="a", package="a"),
        _snapshot(key="b", package="b"),
        _snapshot(key="c", package="c"),
    ]
    controller, calls = _controller(
        tmp_path,
        _Inventory([snapshots]),
        max_actions=2,
    )

    report = controller.run_once()

    assert calls == ["create:a:assemble", "create:b:assemble"]
    assert report.scanned_count == 2
    assert report.action_count == 2
    assert report.next_after_key == "b"


def test_station_failure_is_redacted_bounded_and_does_not_stop_peer(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    integration = _Integration(calls, fail_key="bad")
    observed: list[dict[str, Any]] = []
    controller, _ = _controller(
        tmp_path,
        _Inventory(
            [[_snapshot(key="a", package="bad"), _snapshot(key="b", package="good")]]
        ),
        integration=integration,
        observer=lambda report: observed.append(dict(report)),
    )

    report = controller.run_once()

    assert calls == ["create:bad:assemble", "create:good:assemble"]
    assert report.status == "partial_failure"
    error = str(report.outcomes[0].detail["error"])
    assert "secret" not in error
    assert "abcdef" not in error
    assert "<redacted>" in error
    assert observed[0]["outcomes"][0]["detail"]["error"] == error


def test_published_receipt_is_not_completion_until_atomic_finalizer_succeeds(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    controller, _ = _controller(
        tmp_path,
        _Inventory([[_snapshot(batch="batch", batch_state="published")]]),
        finalization=_Finalization(calls, error=RuntimeError("WIP receipt missing")),
        gates=_Gates(
            {
                "wp:1:1:assemble": PipelineReleaseGate(
                    False, False, reason="new releases disabled"
                )
            }
        ),
    )

    report = controller.run_once()

    assert calls == ["finalize:batch"]
    assert report.outcomes[0].station == "product_finalization"
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].code == "station_failed"


def test_product_finalizer_requires_exact_integrated_and_certified_nodes(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        batch="batch",
        batch_state="published",
        integration_state="ready",
        certification_state="certified",
    )
    controller, calls = _controller(tmp_path, _Inventory([[snapshot]]))

    report = controller.run_once()

    assert calls == []
    assert report.outcomes[0].station == "controller_provenance"
    assert report.outcomes[0].code == "controller_provenance_unready"
    assert "integrated-node provenance" in report.outcomes[0].detail["reason"]


def test_terminal_certification_without_receipt_fails_closed(tmp_path: Path) -> None:
    snapshot = _snapshot(
        batch="batch",
        batch_state="verifying",
        job="job",
        job_state="failed",
    )
    controller, calls = _controller(tmp_path, _Inventory([[snapshot]]))

    report = controller.run_once()

    assert calls == []
    assert report.outcomes[0].code == "terminal_job_missing_certification"
    assert report.action_count == 0


def test_failed_certification_goes_to_atomic_andon_wip_rejection_only(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        batch="batch",
        batch_state="verifying",
        job="job",
        job_state="failed",
        certification="cert",
    )
    controller, calls = _controller(tmp_path, _Inventory([[snapshot]]))

    report = controller.run_once()

    assert calls == ["reject:batch:cert"]
    assert report.outcomes[0].station == "certification_rejection"
    assert report.outcomes[0].status == "advanced"
    assert not any(call.startswith(("accept:", "land:", "finalize:")) for call in calls)


def test_failed_certification_run_validates_rejection_before_paused_package_disappears(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    snapshot = _snapshot(
        batch="batch",
        batch_state="verifying",
        job="job",
        job_state="queued",
    )
    controller, _ = _controller(
        tmp_path,
        _Inventory([[snapshot]]),
        certification=_Certification(calls, run_status="failed"),
        rejection=_Rejection(calls),
    )

    report = controller.run_once()

    assert calls == ["certify:job", "reject:batch:cert"]
    assert report.outcomes[0].station == "certification_rejection"
    assert report.outcomes[0].status == "advanced"


def test_failed_certification_never_claims_completion_without_wip_andon_receipt(
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    snapshot = _snapshot(
        batch="batch",
        batch_state="verifying",
        job="job",
        job_state="failed",
        certification="cert",
    )
    controller, _ = _controller(
        tmp_path,
        _Inventory([[snapshot]]),
        rejection=_Rejection(calls, complete_receipt=False),
    )

    report = controller.run_once()

    assert calls == ["reject:batch:cert"]
    assert report.outcomes[0].status == "failed"
    assert report.outcomes[0].code == "certification_rejection_receipt_incomplete"


def test_rejected_batch_recovers_same_idempotent_wip_andon_station(
    tmp_path: Path,
) -> None:
    snapshot = _snapshot(
        batch="batch",
        batch_state="rejected",
        job="job",
        job_state="failed",
        certification="cert",
    )
    controller, calls = _controller(tmp_path, _Inventory([[snapshot]]))

    report = controller.run_once()

    assert calls == ["reject:batch:cert"]
    assert report.outcomes[0].status == "advanced"


def test_trigger_wakes_background_thread_without_running_inline(tmp_path: Path) -> None:
    called = threading.Event()

    class _SignallingInventory(_Inventory):
        def discover(self, *, after_key: str, limit: int) -> list[PipelineSnapshot]:
            called.set()
            return []

    controller, _ = _controller(
        tmp_path,
        _SignallingInventory([]),
        enabled=True,
        initial_delay=60,
    )
    assert controller.start() is True
    try:
        before = time.monotonic()
        assert controller.trigger() is True
        elapsed = time.monotonic() - before
        assert elapsed < 0.1
        assert called.wait(1.0)
    finally:
        assert controller.stop() is True


def test_service_inventory_discovers_only_ready_groups_and_maps_exact_job() -> None:
    packages = [
        {
            "id": "wp",
            "state": "active",
            "current_plan_version": 2,
            "current_epoch": 3,
        }
    ]
    described = {
        "package": packages[0],
        "plan": {
            "definition": {
                "nodes": [
                    {"node_key": "a", "node_type": "mutation"},
                    {"node_key": "b", "node_type": "mutation"},
                    {"node_key": "c", "node_type": "mutation"},
                    {
                        "node_key": "assemble",
                        "node_type": "integration",
                        "depends_on": ["a", "b"],
                    },
                    {
                        "node_key": "certify",
                        "node_type": "certification",
                        "depends_on": ["assemble"],
                    },
                    {
                        "node_key": "later",
                        "node_type": "integration",
                        "depends_on": ["c"],
                    },
                ],
                "derived": {
                    "integration_groups": [
                        {
                            "integration_node_key": "assemble",
                            "member_node_keys": ["a", "b"],
                        },
                        {
                            "integration_node_key": "later",
                            "member_node_keys": ["c"],
                        },
                    ]
                },
            }
        },
        "nodes": [
            {"node_key": "a", "node_state": "candidate_accepted"},
            {"node_key": "b", "node_state": "candidate_accepted"},
            {"node_key": "c", "node_state": "executing"},
            {
                "node_key": "assemble",
                "task_id": "task_assemble",
                "node_state": "integrated",
                "task_state": "completed",
                "metadata": {
                    "no_dispatch": True,
                    "work_package": {"node_type": "integration"},
                },
            },
            {
                "node_key": "certify",
                "task_id": "task_certify",
                "node_state": "certified",
                "task_state": "completed",
                "metadata": {
                    "no_dispatch": True,
                    "work_package": {"node_type": "certification"},
                },
            },
        ],
        "batches": [
            {
                "id": "batch",
                "plan_version": 2,
                "epoch": 3,
                "state": "verifying",
                "integration_task_id": "task_assemble",
                "metadata": {"integration_node_key": "assemble"},
            }
        ],
    }
    jobs = [
        {
            "id": "job",
            "batch_id": "batch",
            "package_id": "wp",
            "plan_version": 2,
            "epoch": 3,
            "state": "completed",
            "certification_id": "cert",
            "certification_task_id": "task_certify",
        }
    ]
    inventory = ServicePipelineInventory(
        list_packages=lambda **_kw: packages,
        describe_package=lambda _package_id: described,
        list_certification_jobs=lambda **_kw: jobs,
    )

    snapshots = inventory.discover(after_key="", limit=10)

    assert len(snapshots) == 1
    assert snapshots[0].key == "wp:2:3:assemble"
    assert snapshots[0].batch_id == "batch"
    assert snapshots[0].certification_job_id == "job"
    assert snapshots[0].certification_id == "cert"

    jobs[0]["certification_task_id"] = "task_wrong"
    mismatched = inventory.discover(after_key="", limit=10)
    assert len(mismatched) == 1
    assert "exact controller task" in mismatched[0].blocker


def test_service_inventory_uses_held_controller_ready_link_not_worker_claim() -> None:
    package = {
        "id": "wp",
        "state": "active",
        "current_plan_version": 1,
        "current_epoch": 1,
    }
    described = {
        "package": package,
        "plan": {
            "definition": {
                "nodes": [
                    {"node_key": "a", "node_type": "mutation"},
                    {
                        "node_key": "assemble",
                        "node_type": "integration",
                        "depends_on": ["a"],
                    },
                    {
                        "node_key": "certify",
                        "node_type": "certification",
                        "depends_on": ["assemble"],
                    },
                ],
                "derived": {
                    "integration_groups": [
                        {
                            "integration_node_key": "assemble",
                            "member_node_keys": ["a"],
                        }
                    ]
                },
            }
        },
        "nodes": [
            {"node_key": "a", "node_state": "candidate_accepted"},
            {
                "node_key": "assemble",
                "task_id": "task_assemble",
                "node_state": "ready",
                "task_state": "waiting",
                "owner_agent_id": None,
                "lease_id": None,
                "metadata": {
                    "no_dispatch": True,
                    "work_package": {"node_type": "integration"},
                },
            },
            {
                "node_key": "certify",
                "task_id": "task_certify",
                "node_state": "planned",
                "task_state": "waiting",
                "metadata": {
                    "no_dispatch": True,
                    "work_package": {"node_type": "certification"},
                },
            },
        ],
        "batches": [],
    }
    inventory = ServicePipelineInventory(
        list_packages=lambda **_kw: [package],
        describe_package=lambda _package_id: described,
        list_certification_jobs=lambda **_kw: [],
    )

    snapshots = inventory.discover(after_key="", limit=10)

    assert len(snapshots) == 1
    assert snapshots[0].batch_id == ""
    assert snapshots[0].blocker == ""

    described["nodes"][1]["task_state"] = "open"
    unsafe = inventory.discover(after_key="", limit=10)
    assert len(unsafe) == 1
    assert "lost its waiting state" in unsafe[0].blocker


def test_service_inventory_omits_durably_finalized_publication() -> None:
    package = {
        "id": "wp",
        "state": "active",
        "current_plan_version": 1,
        "current_epoch": 1,
    }
    described = {
        "package": package,
        "plan": {
            "definition": {
                "nodes": [
                    {"node_key": "a", "node_type": "mutation"},
                    {
                        "node_key": "assemble",
                        "node_type": "integration",
                        "depends_on": ["a"],
                    },
                    {
                        "node_key": "certify",
                        "node_type": "certification",
                        "depends_on": ["assemble"],
                    },
                ],
                "derived": {
                    "integration_groups": [
                        {
                            "integration_node_key": "assemble",
                            "member_node_keys": ["a"],
                        }
                    ]
                },
            }
        },
        "nodes": [],
        "batches": [
            {
                "id": "batch",
                "plan_version": 1,
                "epoch": 1,
                "state": "published",
                "metadata": {
                    "integration_node_key": "assemble",
                    "product_finalization": {"status": "completed"},
                },
            }
        ],
    }
    inventory = ServicePipelineInventory(
        list_packages=lambda **_kw: [package],
        describe_package=lambda _package_id: described,
        list_certification_jobs=lambda **_kw: [],
    )

    assert inventory.discover(after_key="", limit=10) == ()


def test_paged_service_inventory_reaches_packages_beyond_catalog_limit() -> None:
    store = SQLiteStore(":memory:")
    try:
        store.execute(
            "INSERT INTO project_repositories ("
            "id, name, path, source, project, required_capabilities, enabled, "
            "poll_interval_seconds, metadata, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "repo_catalog",
                "catalog",
                "/tmp/catalog",
                "git@example.invalid:catalog.git",
                "mac",
                "[]",
                1,
                60,
                "{}",
                "created",
                "updated",
            ),
        )
        packages = WorkPackageService(
            store,
            repository_verifier=_CatalogVerifier(),
        )
        expected = {"wp_catalog_%02d" % index for index in range(6)}
        for package_id in sorted(expected):
            admitted = packages.admit(
                {
                    "schema": WORK_PACKAGE_PLAN_SCHEMA,
                    "package_id": package_id,
                    "goal": "prove fair bounded pipeline catalog traversal",
                    "project": "mac",
                    "repository_id": "repo_catalog",
                    "planning_base_ref": "refs/heads/main",
                    "planning_base_sha": "a" * 40,
                    "plan_generation": 1,
                    "nodes": [
                        {
                            "node_key": "change",
                            "title": "Change",
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
                            "depends_on": ["change"],
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
                },
                actor="catalog-test",
                reason="catalog-test",
            )
            packages.activate(
                package_id,
                expected_plan_version=admitted.plan_version,
                expected_epoch=admitted.epoch,
                actor="catalog-test",
            )

        inventory = ServicePipelineInventory(
            list_packages=lambda **kwargs: packages.list(**kwargs),
            describe_package=packages.describe,
            list_certification_jobs=lambda **_kwargs: (),
            catalog_limit=2,
            paged_catalog=True,
        )
        cursor = ""
        observed = set()
        for _tick in range(20):
            snapshot = inventory.discover(after_key=cursor, limit=1)
            assert len(snapshot) == 1
            cursor = snapshot[0].key
            observed.add(snapshot[0].package_id)
            if observed == expected:
                break

        assert observed == expected
        assert "wp_catalog_05" in observed
    finally:
        store.close()


def test_disabled_controller_never_starts(tmp_path: Path) -> None:
    controller, _ = _controller(tmp_path, _Inventory([]))
    assert controller.start() is False
    assert controller.trigger() is False
    assert controller.status()["thread_alive"] is False


def test_control_plane_observer_records_one_bounded_service_event() -> None:
    calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    class _ControlPlane:
        def record_log(self, *args: Any, **kwargs: Any) -> None:
            calls.append((args, kwargs))

    observer = control_plane_pipeline_observer(_ControlPlane())
    observer(
        {
            "schema": "mac.work_package.pipeline_run.v1",
            "status": "partial_failure",
            "outcomes": [],
        }
    )

    assert calls[0][0] == ("work_package.pipeline.run",)
    assert calls[0][1]["level"] == "warning"
    assert calls[0][1]["source"] == "work-package-pipeline"
