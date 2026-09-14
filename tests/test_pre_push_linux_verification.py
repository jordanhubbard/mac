"""Code pushes need a real exact-source Linux result; deferred reports stay honest."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from mac import services, worker


def _repo():
    return {"head_sha": "a" * 40, "dirty": False, "files_changed": ["feature.py"]}


def test_deferred_code_test_cannot_authorize_push():
    problems = worker._repository_finalizer_prepush_problems(
        {}, _repo(), worker._hub_verify_deferred_test_item("test-project"), hub_verify=True
    )
    assert any("passing test" in p for p in problems)


@pytest.mark.parametrize("returncode,blocked", [(0, False), (1, True)])
def test_actual_test_result_controls_prepush(returncode, blocked):
    problems = worker._repository_finalizer_prepush_problems(
        {}, _repo(), {"returncode": returncode}, hub_verify=True
    )
    assert bool(problems) is blocked


def test_stale_head_result_cannot_authorize_push():
    problems = worker._repository_finalizer_prepush_problems(
        {}, _repo(), {"returncode": 0, "executed_head_sha": "b" * 40}, hub_verify=True
    )
    assert any("commit being pushed" in p for p in problems)


@pytest.mark.parametrize(
    "status,returncode,expected",
    [
        ("deferred", None, "deferred"),
        ("unavailable", 1, "unavailable"),
        ("", None, "not run"),
        ("pass", 0, "passed"),
        ("fail", 1, "FAILED"),
    ],
)
def test_activity_preserves_test_outcome(status, returncode, expected):
    assert worker._test_activity_status({"status": status, "returncode": returncode}) == expected


def _git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


@pytest.fixture
def committed_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "feature.txt").write_text("unpublished source\n")
    _git(repo, "add", "feature.txt")
    _git(repo, "commit", "-qm", "unpublished")
    return repo


def test_pre_push_uses_pristine_exact_commit_without_pushing(monkeypatch, committed_repo, tmp_path):
    original_run = subprocess.run
    calls = []
    head = _git(committed_repo, "rev-parse", "HEAD")
    tree = _git(committed_repo, "rev-parse", "HEAD^{tree}")
    (committed_repo / ".git/info/exclude").write_text("ignored-build/\n")
    (committed_repo / "ignored-build").mkdir()
    (committed_repo / "ignored-build/foreign.o").write_bytes(b"host build artifact")
    policy = tmp_path / "policy.yaml"
    policy.write_text("version: 1\n")
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(policy))
    monkeypatch.setenv(
        "MAC_HUB_VERIFY_IMAGE", "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64
    )
    monkeypatch.setenv("MAC_OPENSHELL_BIN", "test-openshell")
    monkeypatch.delenv("MAC_OPENSHELL_GC", raising=False)
    monkeypatch.delenv("MAC_HUB_VERIFY_PROFILE", raising=False)

    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "test-openshell":
            if "create" in argv:
                upload = argv[argv.index("--upload") + 1].removesuffix(":/sandbox")
                import tarfile

                with tarfile.open(upload) as archive:
                    archive.extractall(tmp_path / "received", filter="data")
                received = tmp_path / "received" / "repo"
                assert _git(received, "rev-parse", "HEAD") == head
                assert _git(received, "rev-parse", "HEAD^{tree}") == tree
                assert (received / "feature.txt").read_text() == "unpublished source\n"
                assert not (received / "ignored-build").exists()
                assert "$(uname -s)" in argv[-1] and head in argv[-1] and tree in argv[-1]
                assert "bootstrap-project && test-project" in argv[-1]
                return subprocess.CompletedProcess(argv, 0, "repository tests passed\n", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        # No repository test or bootstrap is ever spawned on the host.
        assert argv[0] in {"git", "tar"}
        return original_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", run)
    result = services.verify_unpublished_repository(
        committed_repo, "test-project", "bootstrap-project"
    )
    assert result["returncode"] == 0 and result["status"] == "pass"
    assert result["executed_head_sha"] == head and result["executed_tree_sha"] == tree
    assert result["execution_environment"] == "openshell_sandbox"
    assert not any("push" in argv for argv in calls)
    assert any("delete" in argv for argv in calls)
    assert (committed_repo / "ignored-build/foreign.o").exists()


@pytest.mark.parametrize("change", ["dirty", "new_commit"])
def test_source_change_during_verification_blocks_push(monkeypatch, committed_repo, change):
    def verify(*args, **kwargs):
        (committed_repo / "feature.txt").write_text("changed during verification\n")
        if change == "new_commit":
            _git(committed_repo, "add", "feature.txt")
            _git(committed_repo, "commit", "-qm", "concurrent change")
        kwargs["verifier_identity"]["execution_attempted"] = True
        return 0, "old source tests passed"

    monkeypatch.setattr(services, "run_repository_contract_test_in_openshell", verify)
    result = services.verify_unpublished_repository(committed_repo, "test-project")
    assert result["returncode"] != 0 and result["status"] != "pass"


def test_missing_runtime_cannot_fall_back_to_host(monkeypatch, committed_repo):
    monkeypatch.delenv("MAC_HUB_VERIFY_IMAGE", raising=False)
    result = services.verify_unpublished_repository(committed_repo, "touch SHOULD_NOT_RUN")
    assert result["returncode"] != 0 and result["status"] == "unavailable"
    assert not (committed_repo / "SHOULD_NOT_RUN").exists()


def test_worker_ignores_workspace_receipt_and_uses_current_contract(
    monkeypatch, committed_repo, tmp_path
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    (task_dir / "mac-sandbox-verification.json").write_text(
        json.dumps(
            {
                "schema": "mac.sandbox_verification.v1",
                "command": "old-test",
                "returncode": 0,
            }
        )
    )
    (task_dir / "task.json").write_text(
        json.dumps(
            {
                "metadata": {
                    "execution_contract": {
                        "repository_contract": {"bootstrap": {"command": "workspace-override"}}
                    }
                }
            }
        )
    )
    calls = []

    def verify(*args, **kwargs):
        calls.append(args)
        return {"returncode": 1, "status": "fail", "command": "current-test"}

    monkeypatch.setattr(services, "verify_unpublished_repository", verify)
    result = worker.MacWorker._run_repository_contract_test(
        None,
        committed_repo,
        "current-test",
        task_dir=task_dir,
        hub_verify=True,
        task={
            "metadata": {
                "execution_contract": {
                    "repository_contract": {"bootstrap": {"command": "bootstrap-current"}}
                }
            }
        },
    )
    assert result["returncode"] == 1
    assert calls == [(committed_repo, "current-test", "bootstrap-current")]


@pytest.mark.parametrize("known_base", [True, False])
def test_pre_push_preserves_trustworthy_affected_scope_and_timeout(
    monkeypatch, committed_repo, known_base
):
    base = _git(committed_repo, "rev-parse", "HEAD")
    (committed_repo / "scripts").mkdir()
    sanity = committed_repo / "scripts/run-sanity-tests.sh"
    sanity.write_text("#!/bin/sh\nexit 0\n")
    sanity.chmod(0o755)
    (committed_repo / "test-policy.toml").write_text("schema_version = 1\n")
    _git(committed_repo, "add", "scripts", "test-policy.toml")
    _git(committed_repo, "commit", "-qm", "sanity contract")
    calls = []

    def verify(*args, **kwargs):
        calls.append((args, kwargs))
        kwargs["verifier_identity"]["execution_attempted"] = True
        return 0, "passed"

    monkeypatch.setattr(services, "run_repository_contract_test_in_openshell", verify)
    monkeypatch.setenv("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "7")
    result = services.verify_unpublished_repository(
        committed_repo,
        "scripts/run-contract-tests.sh",
        prepared_base_sha=base if known_base else "b" * 40,
    )
    assert result["returncode"] == 0
    assert calls[0][0][3] == (
        "scripts/run-sanity-tests.sh --base " + base
        if known_base
        else "scripts/run-contract-tests.sh"
    )
    assert 0 < calls[0][1]["timeout_seconds"] <= 7


def test_staging_consumes_verifier_budget_before_execution(monkeypatch, committed_repo, tmp_path):
    clock = {"now": 100.0}
    calls = []
    monkeypatch.setattr(services.time, "monotonic", lambda: clock["now"])
    monkeypatch.setenv(
        "MAC_HUB_VERIFY_IMAGE", "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64
    )
    monkeypatch.setenv("MAC_OPENSHELL_POLICY", str(tmp_path / "policy.yaml"))
    monkeypatch.delenv("MAC_OPENSHELL_GC", raising=False)

    def run(argv, **kwargs):
        calls.append(argv)
        clock["now"] += 11
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(TimeoutError, match="budget exhausted"):
        services.run_repository_contract_test_in_openshell(
            "",
            "",
            "a" * 40,
            "test-project",
            local_repository=committed_repo,
            expected_tree_sha="b" * 40,
            timeout_seconds=10,
        )
    assert len(calls) == 1 and calls[0][:2] == ["git", "clone"]
