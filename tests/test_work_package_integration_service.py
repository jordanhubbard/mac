from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from mac.models import TransitionError, ValidationError
from mac.store import SQLiteStore
from mac.work_package_integration_service import (
    IntegrationBaseMovedError,
    IntegrationConflictError,
    IntegrationLeaseLostError,
    WorkPackageIntegrationConfig,
    WorkPackageIntegrationService,
)


TARGET_REF = "refs/heads/main"
CREATED_AT = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)


def test_integration_repository_uses_contract_remote_over_legacy_source() -> None:
    repository = WorkPackageIntegrationService._repository_value(
        "repo",
        "git@example.invalid:obsolete/repository.git",
        json.dumps(
            {
                "repository_contract": {
                    "canonical_remote_url": (
                        "ssh://git@example.invalid/current/repository.git"
                    )
                }
            }
        ),
    )

    assert repository.source == "ssh://git@example.invalid/current/repository.git"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout.strip()


def _git_bytes(cwd: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout


def _changed_paths(cwd: Path, base_sha: str, head_sha: str) -> list[str]:
    fields = _git_bytes(
        cwd,
        "diff",
        "--name-status",
        "-z",
        "--find-renames",
        "--find-copies",
        base_sha,
        head_sha,
        "--",
    ).split(b"\x00")
    if fields and fields[-1] == b"":
        fields.pop()
    result: set[str] = set()
    index = 0
    while index < len(fields):
        status = fields[index].decode("ascii")
        index += 1
        count = 2 if status[0] in {"R", "C"} else 1
        for _ in range(count):
            result.add(fields[index].decode("utf-8"))
            index += 1
    return sorted(result)


@dataclass
class _Harness:
    store: SQLiteStore
    remote: Path
    work: Path
    base_sha: str
    heads: dict[str, str]
    attempt_refs: dict[str, str]

    def close(self) -> None:
        self.store.close()


def _repository(
    tmp_path: Path, *, conflict: bool
) -> tuple[Path, Path, str, dict[str, str], dict[str, str]]:
    remote = tmp_path / "canonical.git"
    work = tmp_path / "work"
    remote.mkdir()
    work.mkdir()
    _git(remote, "init", "--bare", "--initial-branch=main")
    _git(work, "init", "--initial-branch=main")
    _git(work, "config", "user.name", "Integration Test")
    _git(work, "config", "user.email", "integration-test@example.invalid")
    (work / "README.md").write_text("base\n", encoding="utf-8")
    (work / "shared.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "base")
    base_sha = _git(work, "rev-parse", "HEAD")
    _git(work, "remote", "add", "origin", str(remote))
    _git(work, "push", "origin", "HEAD:%s" % TARGET_REF)

    attempt_refs = {
        "a": "refs/mac/attempts/wp-integration/e1/a/a1-lease-a",
        "b": "refs/mac/attempts/wp-integration/e1/b/a1-lease-b",
    }
    heads: dict[str, str] = {}
    for node in ("a", "b"):
        _git(work, "checkout", "--detach", base_sha)
        if conflict:
            value = "one\n%s\nthree\n" % ("worker-a" if node == "a" else "worker-b")
            (work / "shared.txt").write_text(value, encoding="utf-8")
        else:
            (work / ("%s.txt" % node)).write_text(
                "component-%s\n" % node, encoding="utf-8"
            )
        _git(work, "add", ".")
        _git(work, "commit", "-m", "component %s" % node)
        heads[node] = _git(work, "rev-parse", "HEAD")
        _git(work, "push", "origin", "HEAD:%s" % attempt_refs[node])
    return remote, work, base_sha, heads, attempt_refs


def _task(
    store: SQLiteStore,
    task_id: str,
    *,
    state: str = "needs_review",
    metadata: dict | None = None,
    dependencies: list[str] | None = None,
) -> None:
    store.execute(
        "INSERT INTO tasks ("
        "id, title, description, priority, state, required_capabilities, dependencies, "
        "metadata, attempt_count, max_attempts, created_at, updated_at"
        ") VALUES (?, ?, '', 0, ?, '[]', ?, ?, 1, 3, ?, ?)",
        (
            task_id,
            task_id,
            state,
            json.dumps(dependencies or [], sort_keys=True),
            json.dumps(metadata or {}, sort_keys=True),
            CREATED_AT.isoformat(),
            CREATED_AT.isoformat(),
        ),
    )


def _seed(
    tmp_path: Path,
    *,
    conflict: bool = False,
    nested_member: bool = False,
    certification_successor: bool = False,
) -> _Harness:
    remote, work, base_sha, heads, attempt_refs = _repository(
        tmp_path, conflict=conflict
    )
    store = SQLiteStore(":memory:")
    now = CREATED_AT.isoformat()
    store.execute(
        "INSERT INTO project_repositories ("
        "id, name, path, source, project, required_capabilities, enabled, "
        "poll_interval_seconds, metadata, created_at, updated_at"
        ") VALUES (?, ?, ?, ?, ?, '[]', 1, 60, '{}', ?, ?)",
        ("repo_integration", "integration", str(remote), str(remote), "mac", now, now),
    )
    store.execute(
        "INSERT INTO work_packages ("
        "id, project, repository_id, goal, state, current_plan_version, current_epoch, "
        "metadata, created_by, created_at, updated_at"
        ") VALUES (?, 'mac', ?, ?, 'draft', 0, 0, '{}', 'planner', ?, ?)",
        ("wp_integration", "repo_integration", "assemble components", now, now),
    )
    nodes = [
        {"node_key": "a", "kind": "mutation", "depends_on": []},
        {
            "node_key": "b",
            "kind": "integration" if nested_member else "mutation",
            "depends_on": [],
        },
        {
            "node_key": "assemble",
            "kind": "integration",
            "depends_on": ["a", "b"],
        },
    ]
    if certification_successor:
        nodes.append(
            {
                "node_key": "certify",
                "kind": "certification",
                "depends_on": ["assemble"],
                "external_dependencies": [],
            }
        )
    definition = {
        "schema": "mac.work_package.plan.v1",
        "package_id": "wp_integration",
        "repository_id": "repo_integration",
        "planning_base_ref": TARGET_REF,
        "planning_base_sha": base_sha,
        "integration": {"target_ref": TARGET_REF},
        "nodes": nodes,
        "derived": {
            "integration_groups": [
                {
                    "integration_node_key": "assemble",
                    "member_node_keys": ["a", "b"],
                    "capacity_scope": "integration:assemble",
                }
            ]
        },
    }
    store.execute(
        "INSERT INTO work_package_plan_versions ("
        "package_id, version, definition, plan_digest, reason, created_by, created_at"
        ") VALUES (?, 1, ?, ?, 'test', 'planner', ?)",
        (
            "wp_integration",
            json.dumps(definition, sort_keys=True),
            "sha256:" + "1" * 64,
            now,
        ),
    )
    store.execute(
        "INSERT INTO work_package_epochs ("
        "package_id, epoch, plan_version, planning_base_ref, planning_base_sha, "
        "status, reason, created_by, created_at"
        ") VALUES (?, 1, 1, ?, ?, 'active', 'test', 'planner', ?)",
        ("wp_integration", TARGET_REF, base_sha, now),
    )
    store.execute(
        "UPDATE work_packages SET state = 'admitted', current_plan_version = 1, "
        "current_epoch = 1 WHERE id = 'wp_integration'"
    )
    store.execute(
        "UPDATE work_packages SET state = 'active' WHERE id = 'wp_integration'"
    )

    materialized_nodes = ("a", "b", "assemble", "certify") if certification_successor else (
        "a",
        "b",
        "assemble",
    )
    for node in materialized_nodes:
        task_id = "task_%s" % node
        metadata = None
        if node == "assemble":
            metadata = {
                "no_dispatch": True,
                "work_package": {
                    "package_id": "wp_integration",
                    "plan_version": 1,
                    "epoch": 1,
                    "node_key": "assemble",
                    "node_type": "integration",
                },
            }
        elif node == "certify":
            metadata = {
                "no_dispatch": True,
                "work_package": {
                    "package_id": "wp_integration",
                    "plan_version": 1,
                    "epoch": 1,
                    "node_key": "certify",
                    "node_type": "certification",
                },
            }
        _task(
            store,
            task_id,
            state=(
                "open"
                if node == "assemble"
                else "waiting" if node == "certify" else "needs_review"
            ),
            metadata=metadata,
            dependencies=["task_assemble"] if node == "certify" else [],
        )
        effects_digest = "sha256:" + (
            {"a": "a", "b": "b", "assemble": "c", "certify": "f"}[node] * 64
        )
        store.execute(
            "INSERT INTO work_package_task_links ("
            "task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "declared_effects_digest, contract_digest, input_digest, node_state, created_at"
            ") VALUES (?, 'wp_integration', 1, 1, ?, 1, ?, ?, ?, 'planned', ?)",
            (
                task_id,
                node,
                effects_digest,
                "sha256:" + "d" * 64,
                "sha256:" + "e" * 64,
                now,
            ),
        )

    for node in ("a", "b"):
        task_id = "task_%s" % node
        lease_id = "lease_%s" % node
        evidence_id = "evidence_%s" % node
        candidate_id = "candidate_%s" % node
        effects_digest = "sha256:" + node * 64
        store.execute(
            "UPDATE work_package_task_links SET node_state = 'ready' WHERE task_id = ?",
            (task_id,),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = 'executing' WHERE task_id = ?",
            (task_id,),
        )
        store.execute(
            "INSERT INTO leases ("
            "id, task_id, agent_id, expires_at, status, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (
                lease_id,
                task_id,
                "agent_%s" % node,
                (CREATED_AT + timedelta(hours=1)).isoformat(),
                now,
                now,
            ),
        )
        store.execute(
            "INSERT INTO work_package_assignment_audit ("
            "lease_id, package_id, plan_version, epoch, node_key, task_id, agent_id, "
            "attempt_number, attempt_ref, attempt_base_ref, attempt_base_sha, "
            "declared_effects_digest, allocator, allocator_version, score, rationale, "
            "decision, created_at"
            ") VALUES (?, 'wp_integration', 1, 1, ?, ?, ?, 1, ?, ?, ?, ?, "
            "'test', 'v1', 1.0, 'test', '{}', ?)",
            (
                lease_id,
                node,
                task_id,
                "agent_%s" % node,
                attempt_refs[node],
                TARGET_REF,
                base_sha,
                effects_digest,
                now,
            ),
        )
        store.execute(
            "INSERT INTO evidence ("
            "id, task_id, kind, uri, summary, metadata, created_by, created_at"
            ") VALUES (?, ?, 'artifact', ?, 'exact attempt', '{}', ?, ?)",
            (
                evidence_id,
                task_id,
                "artifact://%s" % node,
                "agent_%s" % node,
                now,
            ),
        )
        store.execute(
            "INSERT INTO evidence_attempt_links ("
            "evidence_id, task_id, lease_id, agent_id, attempt_number, attempt_ref, "
            "attempt_base_sha, attempt_head_sha, artifact_digest, "
            "declared_effects_digest, protected_ref, created_at"
            ") VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, 1, ?)",
            (
                evidence_id,
                task_id,
                lease_id,
                "agent_%s" % node,
                attempt_refs[node],
                base_sha,
                heads[node],
                "sha256:" + hashlib.sha256(heads[node].encode()).hexdigest(),
                effects_digest,
                now,
            ),
        )
        listing = _git_bytes(work, "ls-tree", "-r", "-z", "--full-tree", heads[node])
        paths = _changed_paths(work, base_sha, heads[node])
        store.execute(
            "INSERT INTO evidence_attempt_verifications ("
            "id, evidence_id, task_id, lease_id, agent_id, attempt_number, "
            "repository_id, attempt_ref, attempt_base_sha, attempt_head_sha, "
            "tree_digest, declared_effects_digest, observed_effects_digest, "
            "changed_paths, changes, verifier, verifier_version, verified_at, "
            "receipt_digest"
            ") VALUES (?, ?, ?, ?, ?, 1, 'repo_integration', ?, ?, ?, ?, ?, ?, ?, "
            "'[]', 'test-verifier', 'v1', ?, ?)",
            (
                "verification_%s" % node,
                evidence_id,
                task_id,
                lease_id,
                "agent_%s" % node,
                attempt_refs[node],
                base_sha,
                heads[node],
                "sha256:" + hashlib.sha256(listing).hexdigest(),
                effects_digest,
                "sha256:" + hashlib.sha256(("effects-" + node).encode()).hexdigest(),
                json.dumps(paths),
                now,
                "sha256:" + hashlib.sha256(("receipt-" + node).encode()).hexdigest(),
            ),
        )
        store.execute(
            "INSERT INTO work_package_node_candidates ("
            "id, task_id, package_id, plan_version, epoch, node_key, node_generation, "
            "assignment_lease_id, attempt_number, evidence_id, status, submitted_at"
            ") VALUES (?, ?, 'wp_integration', 1, 1, ?, 1, ?, 1, ?, 'submitted', ?)",
            (candidate_id, task_id, node, lease_id, evidence_id, now),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = 'candidate_submitted' "
            "WHERE task_id = ?",
            (task_id,),
        )
        store.execute(
            "UPDATE work_package_node_candidates SET status = 'accepted', "
            "accepted_at = ?, accepted_by = 'review-controller' WHERE id = ?",
            (now, candidate_id),
        )
        store.execute(
            "UPDATE work_package_task_links SET node_state = 'candidate_accepted' "
            "WHERE task_id = ?",
            (task_id,),
        )

        mutation_wip = "wip_mutation_%s" % node
        candidate_wip = "wip_candidate_%s" % node
        fan_in_wip = "wpwip_%s" % hashlib.sha256(
            (candidate_wip + "\0" + candidate_id + "\0fan_in_reservation").encode(
                "utf-8"
            )
        ).hexdigest()[:32]
        store.execute(
            "INSERT INTO work_package_wip_tokens ("
            "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
            "token_kind, stage, state, generation, capacity_units, reservation_key, "
            "acquired_by_assignment_lease_id, acquired_at"
            ") VALUES (?, 'wp_integration', 1, 1, ?, ?, ?, 'mutation', 'mutation', "
            "'held', 1, 1, ?, ?, ?)",
            (
                mutation_wip,
                node,
                task_id,
                "repo:slot:%s" % node,
                "assignment:%s" % lease_id,
                lease_id,
                now,
            ),
        )
        store.execute(
            "UPDATE work_package_wip_tokens SET state = 'released', released_at = ?, "
            "release_reason = ? WHERE id = ?",
            (now, "candidate_transfer:%s" % candidate_id, mutation_wip),
        )
        store.execute(
            "INSERT INTO work_package_wip_tokens ("
            "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
            "token_kind, stage, state, generation, capacity_units, reservation_key, "
            "predecessor_token_id, acquired_by_assignment_lease_id, acquired_at"
            ") VALUES (?, 'wp_integration', 1, 1, ?, ?, ?, 'mutation', "
            "'candidate_buffer', 'held', 2, 1, ?, ?, ?, ?)",
            (
                candidate_wip,
                node,
                task_id,
                "repo:slot:%s" % node,
                "assignment:%s" % lease_id,
                mutation_wip,
                lease_id,
                now,
            ),
        )
        acceptance_reason = json.dumps(
            {
                "schema": "mac.work_package.wip_resolution.v1",
                "decision": "accepted",
                "candidate_id": candidate_id,
                "evidence_id": evidence_id,
                "actor": "review-controller",
                "successor_token_id": fan_in_wip,
                "resolved_at": now,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        store.execute(
            "UPDATE work_package_wip_tokens SET state = 'released', released_at = ?, "
            "release_reason = ? WHERE id = ?",
            (now, acceptance_reason, candidate_wip),
        )
        store.execute(
            "INSERT INTO work_package_wip_tokens ("
            "id, package_id, plan_version, epoch, node_key, task_id, resource_key, "
            "token_kind, stage, state, generation, capacity_units, reservation_key, "
            "predecessor_token_id, acquired_by_assignment_lease_id, acquired_at"
            ") VALUES (?, 'wp_integration', 1, 1, ?, ?, ?, 'mutation', "
            "'fan_in_reservation', 'held', 3, 1, ?, ?, ?, ?)",
            (
                fan_in_wip,
                node,
                task_id,
                "repo:slot:%s" % node,
                "assignment:%s" % lease_id,
                candidate_wip,
                lease_id,
                now,
            ),
        )
    assert store.query_all("PRAGMA foreign_key_check") == []
    return _Harness(store, remote, work, base_sha, heads, attempt_refs)


def _service(
    harness: _Harness,
    *,
    owner: str = "integrator-1",
    now: datetime = CREATED_AT,
    fault_hook=None,
) -> WorkPackageIntegrationService:
    return WorkPackageIntegrationService(
        harness.store,
        owner=owner,
        config=WorkPackageIntegrationConfig(lease_seconds=30),
        now=lambda: now,
        fault_hook=fault_hook,
    )


def test_freezes_exact_ordered_membership_and_claim_transfers_bounded_wip(
    tmp_path: Path,
) -> None:
    harness = _seed(tmp_path)
    try:
        service = _service(harness)
        first = service.create_batch("wp_integration", "assemble", actor="controller")
        second = service.create_batch("wp_integration", "assemble", actor="controller")
        assert first.created is True
        assert second.created is False
        assert second.batch_id == first.batch_id
        assert second.input_digest == first.input_digest
        rows = harness.store.query_all(
            "SELECT ordinal, node_key, candidate_id, evidence_id "
            "FROM work_package_batch_inputs WHERE batch_id = ? ORDER BY ordinal",
            (first.batch_id,),
        )
        assert [dict(row) for row in rows] == [
            {
                "ordinal": 0,
                "node_key": "a",
                "candidate_id": "candidate_a",
                "evidence_id": "evidence_a",
            },
            {
                "ordinal": 1,
                "node_key": "b",
                "candidate_id": "candidate_b",
                "evidence_id": "evidence_b",
            },
        ]

        lease = service.claim(first.batch_id)
        assert lease.fence == 1
        integration = harness.store.query_all(
            "SELECT stage, state, reservation_key, predecessor_token_id "
            "FROM work_package_wip_tokens WHERE reservation_key = ? ORDER BY id",
            (first.batch_id,),
        )
        assert len(integration) == 2
        assert all(row["stage"] == "integration" for row in integration)
        assert all(row["state"] == "held" for row in integration)
        assert {row["predecessor_token_id"] for row in integration} == {
            "wpwip_"
            + hashlib.sha256(
                b"wip_candidate_a\0candidate_a\0fan_in_reservation"
            ).hexdigest()[:32],
            "wpwip_"
            + hashlib.sha256(
                b"wip_candidate_b\0candidate_b\0fan_in_reservation"
            ).hexdigest()[:32],
        }
        after_claim_retry = service.create_batch(
            "wp_integration", "assemble", actor="controller"
        )
        assert after_claim_retry.created is False
        assert after_claim_retry.batch_id == first.batch_id
        assert after_claim_retry.input_digest == first.input_digest
        assert harness.store.query_all("PRAGMA foreign_key_check") == []
    finally:
        harness.close()


def test_batch_creation_rejects_legacy_composed_mutation_topology(
    tmp_path: Path,
) -> None:
    harness = _seed(tmp_path)
    try:
        row = harness.store.query_one(
            "SELECT definition FROM work_package_plan_versions "
            "WHERE package_id = 'wp_integration' AND version = 1"
        )
        definition = json.loads(row["definition"])
        b = next(node for node in definition["nodes"] if node["node_key"] == "b")
        b["depends_on"] = ["a"]
        harness.store.execute(
            "DROP TRIGGER trg_work_package_plan_versions_immutable"
        )
        harness.store.execute(
            "UPDATE work_package_plan_versions SET definition = ? "
            "WHERE package_id = 'wp_integration' AND version = 1",
            (json.dumps(definition, sort_keys=True),),
        )

        with pytest.raises(ValidationError, match="flat mutation wave"):
            _service(harness).create_batch(
                "wp_integration", "assemble", actor="controller"
            )

        assert harness.store.query_one(
            "SELECT COUNT(*) AS n FROM work_package_integration_batches"
        )["n"] == 0
    finally:
        harness.close()


def test_status_and_public_results_are_secret_free_integrity_checked_snapshots(
    tmp_path: Path,
) -> None:
    harness = _seed(tmp_path)
    try:
        service = _service(harness)
        creation = service.create_batch(
            "wp_integration", "assemble", actor="controller"
        )
        queued = service.status(creation.batch_id)
        assert queued["state"] == "queued"
        assert queued["input_digest"] == creation.input_digest
        assert [item["node_key"] for item in queued["inputs"]] == ["a", "b"]
        assert queued["held_integration_wip_token_ids"] == []
        assert "source" not in json.dumps(queued)

        lease = service.claim(creation.batch_id)
        claimed = service.status(creation.batch_id)
        assert claimed["state"] == "assembling"
        assert claimed["lease_fence"] == lease.fence == 1
        assert len(claimed["held_integration_wip_token_ids"]) == 2
        assert lease.to_dict() == {
            "batch_id": creation.batch_id,
            "owner": "integrator-1",
            "fence": 1,
            "expires_at": lease.expires_at,
        }

        outcome = service.assemble(creation.batch_id)
        outcome_payload = outcome.to_dict()
        assert outcome_payload["status"] == "assembled"
        assert outcome_payload["batch_id"] == creation.batch_id
        assert outcome_payload["candidate_sha"] == outcome.candidate_sha
        assert outcome_payload["detail"] == {}
    finally:
        harness.close()


def test_assembly_completes_controller_station_and_readies_exact_certification(
    tmp_path: Path,
) -> None:
    harness = _seed(tmp_path, certification_successor=True)
    try:
        service = _service(harness)
        batch = service.create_batch(
            "wp_integration", "assemble", actor="pipeline-controller"
        )
        outcome = service.assemble(batch.batch_id)
        assert outcome.status == "assembled"

        integration = harness.store.query_one(
            "SELECT task.state AS task_state, link.node_state "
            "FROM tasks AS task JOIN work_package_task_links AS link "
            "ON link.task_id = task.id WHERE task.id = 'task_assemble'"
        )
        certification = harness.store.query_one(
            "SELECT task.state AS task_state, task.metadata, link.node_state "
            "FROM tasks AS task JOIN work_package_task_links AS link "
            "ON link.task_id = task.id WHERE task.id = 'task_certify'"
        )
        receipt = harness.store.query_one(
            "SELECT * FROM work_package_controller_station_receipts "
            "WHERE batch_id = ? AND station_kind = 'integration'",
            (batch.batch_id,),
        )
        assert dict(integration) == {
            "task_state": "completed",
            "node_state": "integrated",
        }
        assert certification["task_state"] == "waiting"
        assert certification["node_state"] == "ready"
        assert json.loads(certification["metadata"])["no_dispatch"] is True
        assert receipt["task_id"] == "task_assemble"
        assert receipt["outcome"] == "integrated"
        detail = json.loads(receipt["detail"])
        assert detail["certification_task_id"] == "task_certify"
        assert detail["certification_node_key"] == "certify"
        assert harness.store.query_one(
            "SELECT COUNT(*) AS count FROM task_history WHERE task_id = 'task_assemble' "
            "AND event_type = 'task.transitioned' AND to_state = 'completed'"
        )["count"] == 1
        assert harness.store.query_one(
            "SELECT COUNT(*) AS count FROM task_transition_outbox "
            "WHERE task_id = 'task_assemble' AND event_type = 'task.lifecycle' "
            "AND to_state = 'completed'"
        )["count"] == 1
        assert harness.store.query_all("PRAGMA foreign_key_check") == []
    finally:
        harness.close()


def test_claim_rejects_queued_membership_tampering(tmp_path: Path) -> None:
    harness = _seed(tmp_path)
    try:
        service = _service(harness)
        batch = service.create_batch("wp_integration", "assemble", actor="controller")
        # The compatibility schema permits queued membership edits.  The
        # authoritative claim transaction therefore re-derives the plan
        # frontier and content digest before it transfers any WIP.
        harness.store.execute(
            "DELETE FROM work_package_batch_inputs WHERE batch_id = ? AND ordinal = 1",
            (batch.batch_id,),
        )
        with pytest.raises(TransitionError, match="membership differs|membership"):
            service.claim(batch.batch_id)
        row = harness.store.query_one(
            "SELECT state, lease_owner FROM work_package_integration_batches "
            "WHERE id = ?",
            (batch.batch_id,),
        )
        assert dict(row) == {"state": "queued", "lease_owner": None}
        assert {
            item["id"]
            for item in harness.store.query_all(
                "SELECT id FROM work_package_wip_tokens "
                "WHERE stage = 'fan_in_reservation' AND state = 'held'"
            )
        } == {
            "wpwip_"
            + hashlib.sha256(
                b"wip_candidate_a\0candidate_a\0fan_in_reservation"
            ).hexdigest()[:32],
            "wpwip_"
            + hashlib.sha256(
                b"wip_candidate_b\0candidate_b\0fan_in_reservation"
            ).hexdigest()[:32],
        }
    finally:
        harness.close()


def test_stale_fence_cannot_finalize_and_new_owner_recovers_exact_ref(
    tmp_path: Path,
) -> None:
    harness = _seed(tmp_path)
    try:
        later = CREATED_AT + timedelta(seconds=31)
        owner_two = _service(harness, owner="integrator-2", now=later)
        claimed: list[int] = []

        def steal_after_push(stage: str, detail: dict) -> None:
            if stage == "after_candidate_push_before_finalize":
                claimed.append(owner_two.claim(detail["batch_id"]).fence)

        owner_one = _service(harness, fault_hook=steal_after_push)
        batch = owner_one.create_batch("wp_integration", "assemble", actor="controller")
        with pytest.raises(IntegrationLeaseLostError, match="stale"):
            owner_one.assemble(batch.batch_id)
        assert claimed == [2]
        row = harness.store.query_one(
            "SELECT state, lease_owner, lease_fence, candidate_sha "
            "FROM work_package_integration_batches WHERE id = ?",
            (batch.batch_id,),
        )
        assert dict(row) == {
            "state": "assembling",
            "lease_owner": "integrator-2",
            "lease_fence": 2,
            "candidate_sha": None,
        }

        recovered = owner_two.assemble(batch.batch_id)
        assert recovered.status == "assembled"
        assert recovered.fence == 2
        assert recovered.recovered is True
        finalized = harness.store.query_one(
            "SELECT state, candidate_sha, candidate_fence, lease_owner "
            "FROM work_package_integration_batches WHERE id = ?",
            (batch.batch_id,),
        )
        assert finalized["state"] == "verifying"
        assert finalized["candidate_sha"] == recovered.candidate_sha
        assert finalized["candidate_fence"] == 2
        assert finalized["lease_owner"] is None
    finally:
        harness.close()


def test_moved_canonical_base_is_not_assembled_or_claimed(tmp_path: Path) -> None:
    harness = _seed(tmp_path)
    try:
        service = _service(harness)
        batch = service.create_batch("wp_integration", "assemble", actor="controller")
        _git(harness.work, "checkout", "main")
        (harness.work / "main-moved.txt").write_text("moved\n", encoding="utf-8")
        _git(harness.work, "add", "main-moved.txt")
        _git(harness.work, "commit", "-m", "move canonical base")
        _git(harness.work, "push", "origin", "HEAD:%s" % TARGET_REF)

        with pytest.raises(IntegrationBaseMovedError, match="moved"):
            service.assemble(batch.batch_id)
        row = harness.store.query_one(
            "SELECT state, lease_owner, candidate_sha FROM "
            "work_package_integration_batches WHERE id = ?",
            (batch.batch_id,),
        )
        assert dict(row) == {
            "state": "queued",
            "lease_owner": None,
            "candidate_sha": None,
        }
        assert {
            row["id"]
            for row in harness.store.query_all(
                "SELECT id FROM work_package_wip_tokens "
                "WHERE stage = 'fan_in_reservation' AND state = 'held'"
            )
        } == {
            "wpwip_"
            + hashlib.sha256(
                b"wip_candidate_a\0candidate_a\0fan_in_reservation"
            ).hexdigest()[:32],
            "wpwip_"
            + hashlib.sha256(
                b"wip_candidate_b\0candidate_b\0fan_in_reservation"
            ).hexdigest()[:32],
        }
    finally:
        harness.close()


def test_conflicting_exact_inputs_reject_batch_and_return_product_wip(
    tmp_path: Path,
) -> None:
    harness = _seed(tmp_path, conflict=True)
    try:
        service = _service(harness)
        batch = service.create_batch("wp_integration", "assemble", actor="controller")
        with pytest.raises(IntegrationConflictError, match="overlap|merge"):
            service.assemble(batch.batch_id)
        row = harness.store.query_one(
            "SELECT state, candidate_sha, lease_owner FROM "
            "work_package_integration_batches WHERE id = ?",
            (batch.batch_id,),
        )
        assert dict(row) == {
            "state": "rejected",
            "candidate_sha": None,
            "lease_owner": None,
        }
        returned = harness.store.query_all(
            "SELECT stage, state, predecessor_token_id FROM work_package_wip_tokens "
            "WHERE reservation_key = ? ORDER BY id",
            ("returned:%s" % batch.batch_id,),
        )
        assert len(returned) == 2
        assert all(row["stage"] == "fan_in_reservation" for row in returned)
        assert all(row["state"] == "held" for row in returned)
        assert harness.store.query_all("PRAGMA foreign_key_check") == []
    finally:
        harness.close()


def test_assembly_is_idempotent_and_never_substitutes_mutable_branch_head(
    tmp_path: Path,
) -> None:
    harness = _seed(tmp_path)
    try:
        # A mutable worker branch advances beyond accepted attempt A.  The
        # station must fetch only the protected verification receipt ref/SHA.
        _git(harness.work, "checkout", "--detach", harness.heads["a"])
        (harness.work / "mutable-only.txt").write_text(
            "must not integrate\n", encoding="utf-8"
        )
        _git(harness.work, "add", "mutable-only.txt")
        _git(harness.work, "commit", "-m", "advance mutable worker branch")
        mutable_sha = _git(harness.work, "rev-parse", "HEAD")
        _git(harness.work, "push", "origin", "HEAD:refs/heads/worker-a")
        assert mutable_sha != harness.heads["a"]

        service = _service(harness)
        batch = service.create_batch("wp_integration", "assemble", actor="controller")
        first = service.assemble(batch.batch_id)
        second = service.assemble(batch.batch_id)
        assert first.status == second.status == "assembled"
        assert first.candidate_sha == second.candidate_sha
        assert first.candidate_ref == second.candidate_ref
        assert second.recovered is True
        assert first.candidate_ref.startswith("refs/mac/integration/")
        assert (
            _git(
                harness.remote,
                "show",
                "%s:mutable-only.txt" % first.candidate_sha,
                check=False,
            )
            == ""
        )
        assert (
            subprocess.run(
                [
                    "git",
                    "--git-dir",
                    str(harness.remote),
                    "cat-file",
                    "-e",
                    "%s:mutable-only.txt" % first.candidate_sha,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).returncode
            != 0
        )
        assert (
            _git(harness.remote, "rev-parse", first.candidate_ref)
            == first.candidate_sha
        )
    finally:
        harness.close()


def test_nested_integration_member_fails_closed_until_provenance_schema_exists(
    tmp_path: Path,
) -> None:
    harness = _seed(tmp_path, nested_member=True)
    try:
        with pytest.raises(
            ValidationError,
            match="nested integration inputs are release-blocked",
        ):
            _service(harness).create_batch(
                "wp_integration", "assemble", actor="controller"
            )
        assert (
            harness.store.query_one(
                "SELECT COUNT(*) AS n FROM work_package_integration_batches"
            )["n"]
            == 0
        )
    finally:
        harness.close()
