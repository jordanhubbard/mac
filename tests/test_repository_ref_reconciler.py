from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mac.repository_hygiene import (
    RepositoryHygieneError,
    RepositoryRefAudit,
    RepositoryRefAuditResult,
)
from mac import repository_ref_reconciler as rrr
from mac.repository_ref_reconciler import (
    RepositoryRefReconciler,
    RepositoryRefReconcilerConfig,
)


TASK_ID = "task_" + "a" * 32
LEASE_ID = "lease_" + "b" * 18
SHA = "c" * 40
BRANCH = "mac/agent_worker/%s-%s" % (TASK_ID, LEASE_ID)


class _Repository:
    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return dict(self.data)


class _Plane:
    def __init__(self, repositories=()):
        self.repositories = list(repositories)
        self.logs = []
        self.evidence = []
        self.list_error = None

    def list_project_repositories(self, enabled=None):
        assert enabled is True
        if self.list_error:
            raise self.list_error
        return list(self.repositories)

    def task_detail(self, task_id):
        return {"task": {"id": task_id, "state": "cancelled"}, "history": []}

    def add_evidence(self, *args, **kwargs):
        self.evidence.append((args, kwargs))

    def record_log(self, *args, **kwargs):
        self.logs.append((args, kwargs))


def _repo(path: Path, name="repo", *, metadata=None):
    return _Repository(
        {
            "id": "projectrepo_%s" % name,
            "name": name,
            "project": name,
            "path": str(path),
            "metadata": metadata
            or {
                "repository_contract": {
                    "canonical_remote_url": "git@github.com:example/%s.git" % name
                }
            },
        }
    )


def _audit(eligible=True):
    return RepositoryRefAudit(
        remote="origin",
        branch=BRANCH,
        ref="refs/heads/%s" % BRANCH,
        sha=SHA,
        task_id=TASK_ID,
        lease_id=LEASE_ID,
        task_state="cancelled",
        disposition="not_applicable",
        classification="superseded",
        eligible=eligible,
        eligible_after="2026-06-01T00:00:00+00:00",
        reason="obsolete",
        replacement_task_id=None,
    )


def _install_success_fakes(monkeypatch, *, pr_warning=""):
    calls = {"prune": []}
    monkeypatch.setattr(rrr, "verify_repository_remote", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        rrr,
        "resolve_remote_base_ref",
        lambda *args, **kwargs: "origin/main",
    )
    monkeypatch.setattr(
        rrr,
        "refresh_remote_base_ref",
        lambda _repo, _remote, base_ref: base_ref,
    )
    monkeypatch.setattr(rrr, "list_managed_remote_refs", lambda *args: [object()])
    monkeypatch.setattr(
        rrr,
        "query_open_pull_requests",
        lambda _repo: (None, pr_warning) if pr_warning else ({}, ""),
    )
    monkeypatch.setattr(
        rrr,
        "audit_repository_refs_result",
        lambda *args, **kwargs: RepositoryRefAuditResult(audits=[_audit()]),
    )

    def prune(_repo, audits, *, execute, recorder):
        calls["prune"].append((list(audits), execute, recorder is not None))
        deleted = []
        if execute:
            recorder(audits[0], "requested", "")
            recorder(audits[0], "deleted", "")
            deleted = [audits[0].to_dict()]
        return {"deleted": deleted}

    monkeypatch.setattr(rrr, "prune_repository_refs", prune)
    return calls


def test_config_from_env_is_off_by_default_and_parses_valid_values():
    default = RepositoryRefReconcilerConfig.from_env({})
    assert default.mode == "off"
    assert default.enabled is False

    config = RepositoryRefReconcilerConfig.from_env(
        {
            "MAC_REPOSITORY_REF_RECONCILER_MODE": "PRUNE",
            "MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS": "3600",
            "MAC_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS": "12",
            "MAC_REPOSITORY_REF_RECONCILER_GRACE_DAYS": "2.5",
            "MAC_REPOSITORY_REF_RECONCILER_REMOTE": "upstream",
            "MAC_REPOSITORY_REF_RECONCILER_BASE_REF": "upstream/trunk",
        }
    )
    assert config.enabled is True
    assert config.mode == "prune"
    assert config.interval_seconds == 3600
    assert config.initial_delay_seconds == 12
    assert config.default_grace_seconds == int(2.5 * 24 * 60 * 60)
    assert config.remote == "upstream"
    assert config.base_ref == "upstream/trunk"
    assert config.to_dict()["enabled"] is True


@pytest.mark.parametrize(
    "environ",
    [
        {"MAC_REPOSITORY_REF_RECONCILER_MODE": "delete-everything"},
        {"MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS": "fast"},
        {"MAC_REPOSITORY_REF_RECONCILER_INTERVAL_SECONDS": "1"},
        {"MAC_REPOSITORY_REF_RECONCILER_INITIAL_DELAY_SECONDS": "90000"},
        {"MAC_REPOSITORY_REF_RECONCILER_GRACE_DAYS": "366"},
        {"MAC_REPOSITORY_REF_RECONCILER_REMOTE": "bad remote"},
        {"MAC_REPOSITORY_REF_RECONCILER_BASE_REF": "bad ref"},
    ],
)
def test_invalid_config_fails_closed(environ):
    config = RepositoryRefReconcilerConfig.from_env(environ)
    assert config.mode == "off"
    assert config.enabled is False
    assert config.configuration_error


def test_start_rejects_invalid_or_disabled_configuration():
    plane = _Plane()
    invalid = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(
            mode="off", configuration_error="invalid configuration"
        ),
    )
    assert invalid.start() is False
    assert plane.logs[-1][0][0] == "repository.ref.reconciler.configuration_invalid"

    disabled = RepositoryRefReconciler(plane, RepositoryRefReconcilerConfig())
    assert disabled.start() is False
    assert disabled.run_once()["status"] == "disabled"


def test_audit_mode_never_executes_prune(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    path.mkdir()
    plane = _Plane([_repo(path)])
    calls = _install_success_fakes(monkeypatch)
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit"),
    )

    report = reconciler.run_once(trigger="test")

    assert report["status"] == "completed"
    assert report["mode"] == "audit"
    assert report["eligible_count"] == 1
    assert report["deleted_count"] == 0
    assert calls["prune"][0][1:] == (False, False)
    assert plane.evidence == []
    assert reconciler.status()["last_report"]["run_id"] == report["run_id"]
    assert plane.logs[-1][0][0] == "repository.ref.reconciler.run"


def test_prune_mode_executes_and_records_tombstones(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    path.mkdir()
    plane = _Plane([_repo(path)])
    calls = _install_success_fakes(monkeypatch)
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="prune"),
    )

    report = reconciler.run_once(actor="scheduler", trigger="scheduled")

    assert report["deleted_count"] == 1
    assert calls["prune"][0][1:] == (True, True)
    assert [entry[1]["metadata"]["action"] for entry in plane.evidence] == [
        "requested",
        "deleted",
    ]
    assert all(entry[0][4] == "scheduler" for entry in plane.evidence)


def test_prune_requires_canonical_remote_but_audit_does_not(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    path.mkdir()
    metadata = {"repository_contract": {"canonical_remote_url": None}}
    plane = _Plane([_repo(path, metadata=metadata)])
    _install_success_fakes(monkeypatch)
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="prune"),
    )

    failed = reconciler.run_once()
    assert failed["status"] == "failed"
    assert "canonical_remote_url" in failed["repositories"][0]["error"]

    audited = reconciler.run_once(mode="audit")
    assert audited["status"] == "completed"
    assert audited["repositories"][0]["canonical_remote_verified"] is False


def test_pull_request_failure_warns_in_audit_and_blocks_prune(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    path.mkdir()
    plane = _Plane([_repo(path)])
    _install_success_fakes(monkeypatch, pr_warning="PR state unavailable")
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit"),
    )

    audited = reconciler.run_once()
    assert audited["status"] == "completed_with_warnings"
    assert audited["repositories"][0]["warning"] == "PR state unavailable"

    pruned = reconciler.run_once(mode="prune")
    assert pruned["status"] == "failed"
    assert "refusing executable cleanup" in pruned["repositories"][0]["error"]


def test_repository_opt_out_is_skipped(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    plane = _Plane(
        [
            _repo(
                path,
                metadata={"repository_ref_hygiene": {"enabled": False}},
            )
        ]
    )
    report = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="prune"),
    ).run_once()
    assert report["status"] == "completed"
    assert report["repositories"][0]["status"] == "skipped"


def test_repository_failures_are_isolated_and_errors_are_redacted(
    tmp_path, monkeypatch
):
    bad = tmp_path / "bad"
    good = tmp_path / "good"
    bad.mkdir()
    good.mkdir()
    plane = _Plane([_repo(bad, "bad"), _repo(good, "good")])
    _install_success_fakes(monkeypatch)

    def list_refs(path, _remote):
        if Path(path).name == "bad":
            raise RuntimeError("https://user:secret@example.invalid failed")
        return [object()]

    monkeypatch.setattr(rrr, "list_managed_remote_refs", list_refs)
    report = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit"),
    ).run_once()

    assert report["status"] == "partial_failure"
    assert report["failed_count"] == 1
    assert report["repositories"][1]["status"] == "completed"
    assert "secret" not in report["repositories"][0]["error"]


def test_global_repository_listing_failure_is_reported():
    plane = _Plane()
    plane.list_error = RuntimeError("database unavailable")
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit"),
    )
    report = reconciler.run_once()
    assert report["status"] == "failed"
    assert report["error"] == "database unavailable"
    assert reconciler.status()["last_report"]["status"] == "failed"


def test_concurrent_run_is_reported_busy(tmp_path, monkeypatch):
    path = tmp_path / "repo"
    path.mkdir()
    plane = _Plane([_repo(path)])
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit"),
    )
    entered = threading.Event()
    release = threading.Event()

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(2)
        return {
            "repository_id": "repo",
            "repository": "repo",
            "project": "repo",
            "status": "completed",
            "eligible_count": 0,
            "deleted_count": 0,
        }

    monkeypatch.setattr(reconciler, "_reconcile_repository", blocked)
    worker = threading.Thread(target=reconciler.run_once)
    worker.start()
    assert entered.wait(2)
    assert reconciler.status()["run_active"] is True
    assert reconciler.run_once()["status"] == "busy"
    release.set()
    worker.join(2)
    assert not worker.is_alive()


def test_thread_lifecycle_is_idempotent_and_shutdown_is_prompt():
    plane = _Plane()
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(
            mode="audit",
            interval_seconds=0.01,
            initial_delay_seconds=60,
        ),
    )
    assert reconciler.start() is True
    assert reconciler.start() is False
    assert reconciler.status()["thread_alive"] is True
    assert reconciler.stop(timeout=1) is True
    assert reconciler.status()["thread_alive"] is False


def test_invalid_manual_mode_is_rejected():
    reconciler = RepositoryRefReconciler(
        _Plane(),
        RepositoryRefReconcilerConfig(mode="audit"),
    )
    with pytest.raises(RepositoryHygieneError, match="mode"):
        reconciler.run_once(mode="destroy")


@pytest.mark.parametrize("state", ["stale", "rejected", "cancelled"])
def test_terminal_candidate_refs_become_due_only_from_controller_batches(state):
    class _Store:
        def query_all(self, sql, params=()):
            if "FROM work_package_integration_batches AS batch" in sql:
                return [
                    {
                        "id": "batch_%s" % state,
                        "state": state,
                        "candidate_ref": "refs/mac/integration/batch_%s" % state,
                        "candidate_sha": "d" * 40,
                        "completed_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                        "finalization_id": None,
                    }
                ]
            if "FROM work_package_assignment_audit AS assignment" in sql:
                return []
            if "FROM work_package_batch_inputs AS input" in sql:
                return []
            raise AssertionError(sql)

    plane = _Plane()
    plane.store = _Store()
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit", default_grace_seconds=60),
    )

    authorities = reconciler._protected_ref_authorities(
        "repo_terminal",
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
        grace_seconds=60,
    )

    assert len(authorities) == 1
    assert authorities[0].batch_id == "batch_%s" % state
    assert authorities[0].terminal_state == "batch:%s" % state


def _attempt_ref_row(
    *, candidate_status=None, attempt_head_sha=None, lease_status="released"
):
    return {
        "lease_id": "lease_attempt_terminal",
        "attempt_ref": "refs/mac/attempts/wp/1/node/1/lease_attempt_terminal",
        "task_id": "task_attempt_terminal",
        "plan_version": 1,
        "epoch": 1,
        "node_key": "mutate",
        "attempt_number": 1,
        "lease_status": lease_status,
        "lease_updated_at": "2026-01-01T00:00:00+00:00",
        "task_state": "failed",
        "task_attempt_count": 1,
        "current_task_lease_id": None,
        "task_updated_at": "2026-01-01T00:00:01+00:00",
        "task_completed_at": "2026-01-01T00:00:01+00:00",
        "package_state": "active",
        "current_plan_version": 1,
        "current_epoch": 1,
        "package_updated_at": "2026-01-01T00:00:00+00:00",
        "epoch_status": "active",
        "superseded_at": None,
        "node_state": "rejected" if candidate_status else "cancelled",
        "candidate_id": "candidate_failed" if candidate_status else None,
        "candidate_status": candidate_status,
        "submitted_at": (
            "2026-01-01T00:00:00+00:00" if candidate_status else None
        ),
        "accepted_at": None,
        "attempt_head_sha": attempt_head_sha,
        "verified_at": (
            "2026-01-01T00:00:00+00:00" if attempt_head_sha else None
        ),
    }


@pytest.mark.parametrize(
    ("candidate_status", "lease_status"),
    [("submitted", "released"), (None, "expired")],
)
def test_failed_or_abandoned_attempt_without_observed_head_is_explicit_audit_debt(
    candidate_status,
    lease_status,
):
    class _Store:
        def query_all(self, sql, params=()):
            if "FROM work_package_integration_batches AS batch" in sql:
                return []
            if "FROM work_package_assignment_audit AS assignment" in sql:
                return [
                    _attempt_ref_row(
                        candidate_status=candidate_status,
                        lease_status=lease_status,
                    )
                ]
            if "FROM work_package_batch_inputs AS input" in sql:
                return []
            raise AssertionError(sql)

    plane = _Plane()
    plane.store = _Store()
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit", default_grace_seconds=0),
    )
    debts = []

    authorities = reconciler._protected_ref_authorities(
        "repo_terminal",
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
        grace_seconds=0,
        audit_debts=debts,
    )

    assert authorities == []
    assert len(debts) == 1
    assert debts[0]["reason_code"] == "controller_head_sha_unavailable"
    assert debts[0]["lease_id"] == "lease_attempt_terminal"
    assert debts[0]["terminal_state"] == (
        "candidate:submitted"
        if candidate_status
        else "lease:%s:no_candidate" % lease_status
    )


def test_terminal_verified_attempt_uses_controller_head_without_candidate_n_plus_one():
    calls = []

    class _Store:
        def query_all(self, sql, params=()):
            calls.append(sql)
            if "FROM work_package_integration_batches AS batch" in sql:
                return []
            if "FROM work_package_assignment_audit AS assignment" in sql:
                return [
                    _attempt_ref_row(
                        candidate_status="rejected", attempt_head_sha="e" * 40
                    )
                ]
            if "FROM work_package_batch_inputs AS input" in sql:
                return []
            raise AssertionError(sql)

    plane = _Plane()
    plane.store = _Store()
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit", default_grace_seconds=0),
    )

    authorities = reconciler._protected_ref_authorities(
        "repo_terminal",
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
        grace_seconds=0,
    )

    assert len(authorities) == 1
    assert authorities[0].expected_sha == "e" * 40
    assert authorities[0].terminal_state == "candidate:rejected"
    assert len(calls) == 3


def test_unretirable_attempt_debt_is_visible_in_reconciler_result(monkeypatch, tmp_path):
    plane = _Plane()
    plane.store = object()
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit", default_grace_seconds=0),
    )

    def scan(*_args, **kwargs):
        kwargs["audit_debts"].append(
            {
                "schema": "mac.work_package.ref_retirement_audit_debt.v1",
                "ref": "refs/mac/attempts/unobserved",
                "reason_code": "controller_head_sha_unavailable",
            }
        )
        return []

    monkeypatch.setattr(reconciler, "_protected_ref_authorities", scan)

    result = reconciler._reconcile_protected_work_package_refs(
        repo=tmp_path,
        repository_id="repo",
        remote="origin",
        mode="audit",
        actor="test",
    )

    assert result["eligible_count"] == 0
    assert result["audit_debt_count"] == 1
    assert result["audit_debts"][0]["reason_code"] == (
        "controller_head_sha_unavailable"
    )


def test_failed_protected_ref_delete_is_durable_and_retryable(monkeypatch, tmp_path):
    class _Store:
        def query_one(self, sql, params=()):
            assert "work_package_ref_retirement_receipts" in sql
            return None

    plane = _Plane()
    plane.store = _Store()
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="prune", default_grace_seconds=0),
    )
    authority = rrr._ProtectedRefAuthority(
        repository_id="repo",
        ref_kind="candidate",
        ref="refs/mac/integration/batch-retry",
        expected_sha="d" * 40,
        terminal_state="batch:rejected",
        terminal_at="2026-01-01T00:00:00+00:00",
        eligible_after="2026-01-01T00:00:00+00:00",
        batch_id="batch-retry",
    )
    monkeypatch.setattr(
        reconciler,
        "_protected_ref_authorities",
        lambda *_args, **_kwargs: [authority],
    )
    monkeypatch.setattr(
        reconciler,
        "_ensure_ref_retirement_intent",
        lambda *_args, **_kwargs: "intent-retry",
    )
    recorded = []
    monkeypatch.setattr(
        reconciler,
        "_record_ref_retirement_result",
        lambda intent_id, **values: recorded.append((intent_id, values)),
    )
    attempts = iter([RepositoryHygieneError("remote unavailable"), "deleted"])

    def retire(*_args, **_kwargs):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(rrr, "retire_protected_remote_ref_exact", retire)

    failed = reconciler._reconcile_protected_work_package_refs(
        repo=tmp_path,
        repository_id="repo",
        remote="origin",
        mode="prune",
        actor="test",
    )
    retried = reconciler._reconcile_protected_work_package_refs(
        repo=tmp_path,
        repository_id="repo",
        remote="origin",
        mode="prune",
        actor="test",
    )

    assert failed["failed_count"] == 1
    assert retried["deleted_count"] == 1
    assert [value[1]["outcome"] for value in recorded] == ["failed", "deleted"]


class _StorePlane(_Plane):
    """Control-plane double that also exposes a durable store, as the live
    ControlPlane does, so cursor persistence is exercised."""

    def __init__(self, store, repositories=()):
        super().__init__(repositories)
        self.store = store


def test_last_report_is_persisted_to_the_store(tmp_path, monkeypatch):
    from mac.store import 
from mac.test_support import ephemeral_store

    path = tmp_path / "repo"
    path.mkdir()
    store = ephemeral_store()
    plane = _StorePlane(store, [_repo(path)])
    _install_success_fakes(monkeypatch)
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit"),
    )

    report = reconciler.run_once(trigger="test")

    persisted = store.get_pipeline_cursor(
        "repository_ref_reconciler", "last_report", None
    )
    assert persisted is not None
    assert persisted["run_id"] == report["run_id"]


def test_status_resumes_last_report_from_store_on_restart(tmp_path, monkeypatch):
    from mac.store import 

    path = tmp_path / "repo"
    path.mkdir()
    store = ephemeral_store()
    plane = _StorePlane(store, [_repo(path)])
    _install_success_fakes(monkeypatch)

    first = RepositoryRefReconciler(plane, RepositoryRefReconcilerConfig(mode="audit"))
    report = first.run_once(trigger="test")

    # A fresh reconciler sharing the same durable store (a hub restart) reports
    # the last known result immediately, before any new pass runs.
    resumed = RepositoryRefReconciler(plane, RepositoryRefReconcilerConfig(mode="audit"))
    assert resumed.status()["last_report"] is not None
    assert resumed.status()["last_report"]["run_id"] == report["run_id"]


def test_reconciler_without_store_starts_with_no_last_report(tmp_path):
    plane = _Plane([])
    reconciler = RepositoryRefReconciler(
        plane,
        RepositoryRefReconcilerConfig(mode="audit"),
    )
    assert reconciler.status()["last_report"] is None
