"""Coverage for onboarding detection and environment_contract wiring in MacWorker."""

from __future__ import annotations

from pathlib import Path

from mac import worker


class _Client:
    def __init__(self) -> None:
        self.posts = []

    def post(self, path, payload):
        self.posts.append((path, payload))
        return {}

    def get(self, _path):
        return {}


def _worker(tmp_path):
    return worker.MacWorker(
        _Client(),
        "agent",
        tmp_path,
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


def _observation_names(client):
    return [
        payload.get("name")
        for path, payload in client.posts
        if path == "/observability/logs"
    ]


def test_is_onboarding_task_true_when_origin_onboarding_flag(tmp_path) -> None:
    instance = _worker(tmp_path)
    task = {"metadata": {"origin": {"onboarding": True}}}
    assert instance._is_onboarding_task(task) is True


def test_is_onboarding_task_true_when_contract_missing_schema(tmp_path) -> None:
    instance = _worker(tmp_path)
    task = {"metadata": {"origin": {"repository_contract": {}}}}
    assert instance._is_onboarding_task(task) is True
    task_no_contract = {"metadata": {"origin": {"source": "mac-hub"}}}
    assert instance._is_onboarding_task(task_no_contract) is True


def test_is_onboarding_task_false_when_valid_contract_schema(tmp_path) -> None:
    instance = _worker(tmp_path)
    task = {
        "metadata": {
            "origin": {
                "repository_contract": {"schema": "mac.repository_contract.v1"}
            }
        }
    }
    assert instance._is_onboarding_task(task) is False


def test_is_onboarding_task_false_when_metadata_or_origin_missing(tmp_path) -> None:
    instance = _worker(tmp_path)
    assert instance._is_onboarding_task({}) is False
    assert instance._is_onboarding_task({"metadata": {}}) is False
    assert instance._is_onboarding_task({"metadata": {"origin": None}}) is False


def test_prepare_task_workspace_writes_environment_contract(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    repository_context = {"repository_worktree": str(worktree_dir)}

    monkeypatch.setattr(
        instance, "_prepare_repository_worktree", lambda *_a, **_k: repository_context
    )
    env_contract = {"schema": "mac.environment_contract.v1", "preflight": {"status": "ready"}}
    monkeypatch.setattr(worker, "derive_environment_contract", lambda _dir: env_contract)
    monkeypatch.setattr(worker, "validate_environment_contract", lambda contract: contract)

    task = {"id": "task_x", "metadata": {"origin": {"onboarding": True}}}
    task_dir = instance._prepare_task_workspace(task, {"id": "lease_x"})

    contract_path = task_dir / "environment-contract.json"
    assert contract_path.is_file()
    assert "mac.environment_contract.v1" in contract_path.read_text(encoding="utf-8")


def test_prepare_task_workspace_populates_runtime_environment_contract(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    repository_context = {"repository_worktree": str(worktree_dir)}

    monkeypatch.setattr(
        instance, "_prepare_repository_worktree", lambda *_a, **_k: repository_context
    )
    env_contract = {"schema": "mac.environment_contract.v1", "preflight": {"status": "ready"}}
    monkeypatch.setattr(worker, "derive_environment_contract", lambda _dir: env_contract)
    monkeypatch.setattr(worker, "validate_environment_contract", lambda contract: contract)

    task = {"id": "task_x", "metadata": {"origin": {"onboarding": True}}}
    instance._prepare_task_workspace(task, {"id": "lease_x"})

    assert task["metadata"]["runtime"]["environment_contract"] == env_contract


def test_prepare_task_workspace_fires_derived_log(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    repository_context = {"repository_worktree": str(worktree_dir)}

    monkeypatch.setattr(
        instance, "_prepare_repository_worktree", lambda *_a, **_k: repository_context
    )
    env_contract = {"schema": "mac.environment_contract.v1", "preflight": {"status": "ready"}}
    monkeypatch.setattr(worker, "derive_environment_contract", lambda _dir: env_contract)
    monkeypatch.setattr(worker, "validate_environment_contract", lambda contract: contract)

    task = {"id": "task_x", "metadata": {"origin": {"onboarding": True}}}
    instance._prepare_task_workspace(task, {"id": "lease_x"})

    names = _observation_names(instance.client)
    assert "worker.environment_contract.derived" in names
    derived = next(
        payload
        for _path, payload in instance.client.posts
        if payload.get("name") == "worker.environment_contract.derived"
    )
    assert derived["detail"]["status"] == "ready"


def test_prepare_task_workspace_derives_for_non_onboarding_repository_tasks(
    monkeypatch, tmp_path
) -> None:
    """Derivation is NOT onboarding-only.

    It was, until the contract became the input to per-repo sandbox egress
    rendering (ADR 0009 §2a). It is ordinary coding tasks — not onboarding —
    that run `pnpm install` and need the registry reachable, so deriving only at
    onboarding left every later task with no contract for the executor to widen
    egress from. The derived contract still carries only repo trust; the
    executor classifies it before any host is granted.
    """
    instance = _worker(tmp_path)
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    repository_context = {"repository_worktree": str(worktree_dir)}

    monkeypatch.setattr(
        instance, "_prepare_repository_worktree", lambda *_a, **_k: repository_context
    )
    env_contract = {
        "schema": "mac.environment_contract.v1",
        "preflight": {"status": "ready"},
        "egress": {"hosts": ["registry.npmjs.org"]},
    }
    monkeypatch.setattr(worker, "derive_environment_contract", lambda _dir: env_contract)
    monkeypatch.setattr(worker, "validate_environment_contract", lambda contract: contract)

    task = {
        "id": "task_x",
        "metadata": {
            "origin": {
                "repository_contract": {"schema": "mac.repository_contract.v1"}
            }
        },
    }
    assert instance._is_onboarding_task(task) is False
    task_dir = instance._prepare_task_workspace(task, {"id": "lease_x"})

    assert (task_dir / "environment-contract.json").is_file()
    assert task["metadata"]["runtime"]["environment_contract"] == env_contract
    derived = next(
        payload
        for _path, payload in instance.client.posts
        if payload.get("name") == "worker.environment_contract.derived"
    )
    assert derived["detail"]["onboarding"] is False
    assert derived["detail"]["egress_hosts_proposed"] == 1


def test_prepare_task_workspace_skips_when_task_has_no_repository(
    monkeypatch, tmp_path
) -> None:
    """No repository worktree, nothing to analyse."""
    instance = _worker(tmp_path)
    monkeypatch.setattr(
        instance, "_prepare_repository_worktree", lambda *_a, **_k: None
    )

    def _fail(_dir):
        raise AssertionError("derive_environment_contract should not be called")

    monkeypatch.setattr(worker, "derive_environment_contract", _fail)

    task_dir = instance._prepare_task_workspace(
        {"id": "task_x", "metadata": {}}, {"id": "lease_x"}
    )
    assert not (task_dir / "environment-contract.json").exists()


def test_prepare_task_workspace_fires_derivation_failed_log(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    worktree_dir = tmp_path / "worktree"
    worktree_dir.mkdir()
    repository_context = {"repository_worktree": str(worktree_dir)}

    monkeypatch.setattr(
        instance, "_prepare_repository_worktree", lambda *_a, **_k: repository_context
    )

    def _boom(_dir):
        raise RuntimeError("derive failure")

    monkeypatch.setattr(worker, "derive_environment_contract", _boom)

    task = {"id": "task_x", "metadata": {"origin": {"onboarding": True}}}
    task_dir = instance._prepare_task_workspace(task, {"id": "lease_x"})

    names = _observation_names(instance.client)
    assert "worker.environment_contract.derivation_failed" in names
    assert not (task_dir / "environment-contract.json").exists()


def test_prepare_task_workspace_skips_when_worktree_dir_missing(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    missing_dir = tmp_path / "does-not-exist"
    repository_context = {"repository_worktree": str(missing_dir)}

    monkeypatch.setattr(
        instance, "_prepare_repository_worktree", lambda *_a, **_k: repository_context
    )

    def _fail(_dir):
        raise AssertionError("derive_environment_contract should not be called")

    monkeypatch.setattr(worker, "derive_environment_contract", _fail)

    task = {"id": "task_x", "metadata": {"origin": {"onboarding": True}}}
    task_dir = instance._prepare_task_workspace(task, {"id": "lease_x"})

    assert not (task_dir / "environment-contract.json").exists()
