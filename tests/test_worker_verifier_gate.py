"""The worker's pre-push verifier is the single test gate, so only its results count.

Over 90 days, 64% of the 473 changes the hub's second test run rejected carried
a worker "pass" for which no test had run: 265 from a sandbox shortcut that
reported a clean ``git status`` (the agent had committed) as a pass, 27 deferred
placeholders, 18 agent-written or host-run results. A repository change now
passes only on a test item the pre-push verifier itself recorded for the exact
commit being published, and both the worker (before pushing) and the hub (when
accepting evidence) apply the same rule.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from mac import services, worker
from mac.evidence_validators import (
    validate_evidence_type,
    verifier_test_item_problems,
    verifier_tests_problems,
)
from mac.gitops import canonical_sync_selection_base
from mac.services import ControlPlane
from tests.conftest import verifier_test_item

HEAD = "a" * 40
OTHER = "b" * 40


def _bad_items():
    clean_tree_receipt = {
        # What the deleted clean-tree shortcut wrote, as _process_check_item
        # used to turn it into a test item: returncode 0, skipped dropped.
        "name": "repository contract test",
        "command": "scripts/run-contract-tests.sh",
        "returncode": 0,
        "status": "pass",
        "execution_environment": "openshell_sandbox",
        "stdout": "",
        "stderr": "",
    }
    return {
        "missing_executed_head_sha": verifier_test_item(HEAD, executed_head_sha=""),
        "mismatched_executed_head_sha": verifier_test_item(OTHER),
        "missing_executed_tree_sha": verifier_test_item(HEAD, executed_tree_sha=""),
        "deferred": worker._hub_verify_deferred_test_item("scripts/run-contract-tests.sh"),
        "skipped": verifier_test_item(HEAD, skipped=True),
        "clean_tree_sandbox_receipt": clean_tree_receipt,
        "agent_written": {"name": "pytest", "command": "pytest -q", "returncode": 0},
        "agent_written_status_only": {"command": "make test", "status": "passed"},
        "native_host_run": verifier_test_item(HEAD, execution_environment="host"),
        "failed": verifier_test_item(HEAD, returncode=1, status="fail"),
        "no_output_or_count": verifier_test_item(HEAD, stdout="", test_count=None),
    }


BAD = _bad_items()


def _repo(head=HEAD):
    return {"head_sha": head, "dirty": False, "files_changed": ["feature.py"]}


def _manifest(tests, head=HEAD, files_changed=("feature.py",)):
    return {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "repo_change",
        "repo": {
            "head_sha": head,
            "pushed": True,
            "remote_ref": "refs/heads/task/feature",
            "dirty": False,
            "files_changed": list(files_changed),
        },
        "tests": tests,
    }


# --- the predicate -----------------------------------------------------------


def test_a_genuine_verifier_pass_is_accepted():
    assert verifier_test_item_problems(verifier_test_item(HEAD), HEAD) == []
    # A kvm verifier is a verifier too; a parsed count stands in for output.
    assert (
        verifier_test_item_problems(
            verifier_test_item(HEAD, execution_environment="dedicated_kvm", stdout=""), HEAD
        )
        == []
    )


@pytest.mark.parametrize("kind", sorted(BAD))
def test_each_non_verifier_item_is_rejected(kind):
    assert verifier_test_item_problems(BAD[kind], HEAD)


def test_one_genuine_item_among_others_suffices():
    assert (
        verifier_tests_problems(_manifest([BAD["agent_written"], verifier_test_item(HEAD)])) == []
    )
    assert verifier_tests_problems(_manifest([BAD["agent_written"]]))
    assert verifier_tests_problems(_manifest([]))


# --- worker: before pushing --------------------------------------------------


@pytest.mark.parametrize("kind", sorted(BAD))
def test_worker_prepush_refuses_each_non_verifier_item(kind):
    problems = worker._repository_finalizer_prepush_problems({}, _repo(), BAD[kind])
    assert problems, kind


def test_worker_prepush_accepts_a_genuine_verifier_pass():
    assert (
        worker._repository_finalizer_prepush_problems({}, _repo(), verifier_test_item(HEAD)) == []
    )


@pytest.mark.parametrize("kind", sorted(BAD))
def test_worker_submission_check_refuses_each_non_verifier_item(kind):
    problems = worker._worker_verification_contract_problems(
        _manifest([BAD[kind]]), "repo_change", require_verifier_tests=True
    )
    assert any("repository verifier test result" in p for p in problems), problems


def test_worker_submission_check_accepts_a_verifier_pass_and_contractless_repos():
    assert (
        worker._worker_verification_contract_problems(
            _manifest([verifier_test_item(HEAD)]), "repo_change", require_verifier_tests=True
        )
        == []
    )
    # No test command in the contract: an executor's own result still counts.
    assert (
        worker._worker_verification_contract_problems(
            _manifest([BAD["agent_written"]]), "repo_change", require_verifier_tests=False
        )
        == []
    )


def test_skipped_sandbox_receipt_is_carried_through_as_not_passing(tmp_path):
    (tmp_path / "mac-sandbox-verification.json").write_text(
        json.dumps(
            {
                "schema": "mac.sandbox_verification.v1",
                "status": "skipped",
                "command": "make test",
                "returncode": 0,
                "skipped": True,
                "skipped_reason": "HEAD is the uploaded baseline and the worktree is clean",
            }
        )
    )
    item = worker._sandbox_repository_verification_item(tmp_path, "make test")
    assert item is not None
    assert item["skipped"] is True and item["status"] == "skipped"
    assert item["returncode"] is None
    assert worker._worker_verification_item_passed(item) is False
    assert worker._repository_finalizer_prepush_problems({}, _repo(), item)


def _repository_task(command="make test", **metadata):
    contract = {"schema": "mac.repository_contract.v1"}
    if command:
        contract["test"] = {"command": command}
    return {"metadata": {"origin": {"repository_contract": contract}, **metadata}}


def test_agent_written_manifest_is_refinalized_through_the_verifier(tmp_path):
    path = tmp_path / "mac-evidence.json"
    path.write_text(json.dumps(_manifest([BAD["agent_written"]])))
    assert worker._agent_manifest_lacks_verifier_tests(path, _repository_task()) is True

    path.write_text(json.dumps(_manifest([verifier_test_item(HEAD)])))
    assert worker._agent_manifest_lacks_verifier_tests(path, _repository_task()) is False

    # A verifier FAIL is a verdict, not a missing result: do not rerun it.
    path.write_text(json.dumps(_manifest([BAD["failed"]])))
    assert worker._agent_manifest_lacks_verifier_tests(path, _repository_task()) is False

    # No contract tests, or a non-repository outcome: the manifest stands.
    path.write_text(json.dumps(_manifest([BAD["agent_written"]])))
    assert worker._agent_manifest_lacks_verifier_tests(path, _repository_task("")) is False
    no_change = dict(_manifest([BAD["agent_written"]]), evidence_type="no_change")
    path.write_text(json.dumps(no_change))
    assert worker._agent_manifest_lacks_verifier_tests(path, _repository_task()) is False


# --- hub: accepting evidence -------------------------------------------------


@pytest.mark.parametrize("kind", sorted(BAD))
def test_hub_validator_refuses_each_non_verifier_item(kind):
    problems = validate_evidence_type(
        "repo_change",
        _manifest([BAD[kind]]),
        passed_check_count=lambda _m: 1,
        require_verifier_tests=True,
    )
    assert any("repository verifier test result" in p for p in problems), problems


def test_hub_validator_accepts_a_genuine_verifier_pass():
    assert (
        validate_evidence_type(
            "repo_change",
            _manifest([verifier_test_item(HEAD)]),
            passed_check_count=lambda _m: 1,
            require_verifier_tests=True,
        )
        == []
    )


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def _hub_task(cp, command="make test", **extra_metadata):
    contract = {"schema": "mac.repository_contract.v1", "default_branch": "main"}
    if command:
        contract["test"] = {"command": command}
    return cp.create_task(
        "verifier gate",
        metadata={"origin": {"repository_contract": contract}, **extra_metadata},
    )


@pytest.mark.parametrize("kind", sorted(BAD))
def test_hub_completion_validation_refuses_each_non_verifier_item(cp, kind):
    task = _hub_task(cp)
    problems = cp._verification_type_problems(task, _manifest([BAD[kind]]), "repo_change")
    assert any("repository verifier test result" in p for p in problems), problems


def test_hub_completion_validation_accepts_a_verifier_pass(cp):
    task = _hub_task(cp)
    assert (
        cp._verification_type_problems(task, _manifest([verifier_test_item(HEAD)]), "repo_change")
        == []
    )


def test_hub_leaves_contractless_repos_and_evidence_only_tasks_alone(cp):
    # A repository whose contract defines no test command.
    contractless = _hub_task(cp, command="")
    assert (
        cp._verification_type_problems(
            contractless, _manifest([BAD["agent_written"]]), "repo_change"
        )
        == []
    )
    # A plain task with no repository contract at all.
    plain = cp.create_task("plain")
    assert (
        cp._verification_type_problems(plain, _manifest([BAD["agent_written"]]), "repo_change")
        == []
    )
    # Evidence-only outcomes on a tested repository are not repo_change.
    tested = _hub_task(cp)
    no_change = {
        "schema": "mac.worker_evidence.v1",
        "status": "complete",
        "evidence_type": "no_change",
        "reason": "already implemented on main",
        "repo": {"head_sha": HEAD, "dirty": False, "files_changed": []},
        "checks": [{"name": "inspection", "returncode": 0}],
    }
    assert cp._verification_type_problems(tested, no_change, "no_change") == []


def test_hub_no_longer_counts_deferred_or_skipped_items_as_passing(cp):
    assert cp._verification_item_passed({"status": "deferred", "returncode": 0}) is False
    assert cp._verification_item_passed({"returncode": 0, "skipped": True}) is False
    assert cp._verification_item_passed({"returncode": 0}) is True


# --- selection is deterministic and recorded --------------------------------


def test_selection_base_is_the_post_rebase_canonical_tip():
    tip = "c" * 40
    prepared = "d" * 40
    assert (
        canonical_sync_selection_base({"status": "rebased", "canonical_tip": tip}, prepared) == tip
    )
    assert canonical_sync_selection_base({"status": "fresh", "canonical_tip": tip}, prepared) == tip
    # No trustworthy tip: the prepared base (the selector escalates if unusable).
    for sync in ({"status": "conflict", "canonical_tip": tip}, {"status": "skipped"}, None):
        assert canonical_sync_selection_base(sync, prepared) == prepared


def test_verifier_records_selection_output_edges_and_test_count():
    output = (
        "sanity selection: full (no_changed_file_scope)\n"
        + "x" * 5000
        + "\n=========== 7 passed, 2 skipped in 3.10s ===========\n"
    )
    record = services._verifier_output_record(output)
    assert record["selection"] == "sanity selection: full (no_changed_file_scope)"
    assert record["selection_mode"] == "full"
    assert record["selection_reason"] == "no_changed_file_scope"
    assert record["selection_fallback_full"] is True
    assert record["test_count"] == 9
    assert record["output_head"].startswith("sanity selection:")
    assert record["output_tail"].rstrip().endswith("===========")
    assert len(record["output_head"]) <= 2000 and len(record["output_tail"]) <= 2000
    assert record["output_chars"] == len(output)

    focused = services._verifier_output_record(
        "sanity selection: focused (impact_hybrid_scope)\n12 passed in 1.0s\n"
    )
    assert "selection_fallback_full" not in focused
    assert focused["test_count"] == 12


def _git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True).strip()


def test_verify_unpublished_repository_stores_the_record(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "t")
    _git(repo, "config", "user.email", "t@example.invalid")
    (repo / "f.txt").write_text("x\n")
    _git(repo, "add", "f.txt")
    _git(repo, "commit", "-qm", "c")

    def run(*_args, **kwargs):
        kwargs["verifier_identity"]["execution_attempted"] = True
        return 0, "sanity selection: focused (impact_hybrid_scope)\n4 passed in 0.5s\n"

    monkeypatch.setattr(services, "run_repository_contract_test_in_openshell", run)
    result = services.verify_unpublished_repository(repo, "make test")
    assert result["status"] == "pass"
    assert result["executed_head_sha"] == _git(repo, "rev-parse", "HEAD")
    assert result["selection"] == "sanity selection: focused (impact_hybrid_scope)"
    assert result["test_count"] == 4
    assert verifier_test_item_problems(result, result["executed_head_sha"]) == []


def test_worker_fallback_passes_its_selection_base_to_the_verifier(monkeypatch, tmp_path):
    calls = []

    def verify(*args, **kwargs):
        calls.append(kwargs)
        return {"returncode": 1, "status": "fail"}

    monkeypatch.setattr(services, "verify_unpublished_repository", verify)
    worker.MacWorker._run_repository_contract_test(
        None, tmp_path, "make test", task={}, selection_base_sha="c" * 40
    )
    assert calls[0]["selection_base_sha"] == "c" * 40


# --- landing order: rebase -> verify -> guarded_push on the same commit ------


def _finalizer_repo(tmp_path, monkeypatch):
    from tests.test_task_executor import _prepare_finalizer_env, _setup_two_repo_worktree

    _origin, canonical, work, _main_sha = _setup_two_repo_worktree(tmp_path)
    advance = tmp_path / "advance"
    subprocess.run(["git", "clone", "-q", canonical.as_uri(), str(advance)], check=True)
    _git(advance, "config", "user.email", "t@t")
    _git(advance, "config", "user.name", "t")
    (advance / "peer.py").write_text("# landed meanwhile\n")
    _git(advance, "add", "-A")
    _git(advance, "commit", "-qm", "peer landed")
    _git(advance, "push", "-q", "origin", "main")
    tip = _git(advance, "rev-parse", "HEAD")
    ws = tmp_path / "ws"
    ws.mkdir()
    _prepare_finalizer_env(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_TASK_REPO_WORKTREE", str(work))
    task = {
        "id": "t-gate",
        "metadata": {
            "publication_target": "git://main",
            "origin": {
                "repository_contract": {
                    "canonical_remote_url": canonical.as_uri(),
                    "test": {"command": "scripts/run-contract-tests.sh"},
                }
            },
        },
    }
    return canonical, work, ws, task, tip


def test_finalizer_selects_against_the_post_rebase_tip_and_pushes_the_verified_head(
    tmp_path, monkeypatch
):
    from mac import executor_finalizer

    canonical, work, ws, task, tip = _finalizer_repo(tmp_path, monkeypatch)
    calls = []

    def verify(worktree, command, bootstrap="", **kwargs):
        head = _git(worktree, "rev-parse", "HEAD")
        calls.append({"head": head, **kwargs})
        return verifier_test_item(
            head, command=command, executed_tree_sha=_git(worktree, "rev-parse", "HEAD^{tree}")
        )

    monkeypatch.setattr(services, "verify_unpublished_repository", verify)
    executor_finalizer.run_deterministic_git_finalizer(ws, task)
    manifest = json.loads((ws / "mac-evidence.json").read_text())

    assert manifest["repo"]["canonical_sync"]["status"] == "rebased"
    assert calls and calls[0]["selection_base_sha"] == tip
    assert manifest["repo"]["pushed"] is True
    pushed = _git(canonical, "rev-parse", "refs/heads/task/feature")
    assert pushed == calls[0]["head"] == manifest["repo"]["head_sha"]
    assert manifest["tests"][0]["executed_head_sha"] == pushed


@pytest.mark.parametrize("kind", ["missing_executed_head_sha", "mismatched_executed_head_sha"])
def test_finalizer_does_not_push_without_a_verifier_result_for_its_head(
    tmp_path, monkeypatch, kind
):
    from mac import executor_finalizer

    canonical, work, ws, task, _tip = _finalizer_repo(tmp_path, monkeypatch)
    monkeypatch.setattr(services, "verify_unpublished_repository", lambda *a, **k: dict(BAD[kind]))
    executor_finalizer.run_deterministic_git_finalizer(ws, task)
    manifest = json.loads((ws / "mac-evidence.json").read_text())
    assert manifest["repo"]["pushed"] is False
    branch = subprocess.run(
        ["git", "-C", str(canonical), "rev-parse", "--verify", "refs/heads/task/feature"],
        capture_output=True,
    )
    assert branch.returncode != 0


def test_unchanged_baseline_skip_is_never_a_pass():
    from mac.executor_sandbox import _sandbox_repository_verification_shell

    script = _sandbox_repository_verification_shell(
        {"MAC_TASK_WORKSPACE": "/w", "MAC_REPO_TEST_COMMAND": "make test"}
    )
    assert "elif _no_changes:" not in script
    _head, _sep, tail = script.partition("elif _unchanged_baseline:")
    assert '"status": "skipped"' in tail.split("else:", 1)[0]


def test_read_only_report_lane_still_trusts_only_its_own_verifier(tmp_path):
    """Unchanged by this gate: the report lane reads its own trusted record."""
    task = {
        "metadata": {
            "execution_contract": {
                "repository_contract": {"test": {"command": "make test"}},
            }
        }
    }
    item, problems = worker._trusted_read_only_report_test_item(tmp_path, task)
    assert item is None and problems
