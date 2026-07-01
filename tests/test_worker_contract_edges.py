"""Coverage for worker verification and operating-system boundaries."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from mac import worker


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_execution_environment_summary_collects_bootstrap_and_deltas(tmp_path) -> None:
    assert worker.MacWorker._execution_env_summary(object(), tmp_path) == ""
    (tmp_path / "mac-evidence.json").write_text("not-json")
    assert worker.MacWorker._execution_env_summary(object(), tmp_path) == ""
    (tmp_path / "mac-evidence.json").write_text(
        json.dumps(
            {
                "bootstrap": {"command": "uv sync", "returncode": 2},
                "tests": [
                    "invalid",
                    {"environment_delta": "invalid"},
                    {
                        "environment_delta": {
                            "installed": ["one", "two"],
                            "added": ["three"],
                            "missing": ["four"],
                            "missing_commands": ["five"],
                        }
                    },
                ],
            }
        )
    )
    assert worker.MacWorker._execution_env_summary(object(), tmp_path) == (
        "bootstrap: uv sync (rc 2); installed: one, two; added: three; missing: four"
    )


def test_verification_contract_dispatches_all_evidence_types(monkeypatch) -> None:
    monkeypatch.setattr(worker, "codegraph_audit_manifest_problems", lambda _manifest: ["cg"])
    sha = "a" * 40
    anchor = {
        "repo": {
            "head_sha": sha,
            "dirty": False,
            "pushed": True,
            "remote_ref": "refs/heads/task",
            "files_changed": ["src/a.py"],
        },
        "tests": [{"returncode": 0}],
    }
    assert worker._worker_verification_contract_problems(anchor, "repo_change") == ["cg"]
    assert worker._worker_verification_contract_problems(anchor, "documentation") == ["cg"]

    deployment = worker._worker_verification_contract_problems({}, "deployment")
    assert "deployment evidence requires at least one passing check" in deployment
    assert "deployment evidence requires targets, services, or artifacts" in deployment
    assert "cg" in deployment

    test_problems = worker._worker_verification_contract_problems({}, "test")
    assert "test evidence requires at least one passing check or test" in test_problems
    artifact = worker._worker_verification_contract_problems({}, "artifact")
    assert "artifact evidence requires artifacts" in artifact
    no_change = worker._worker_verification_contract_problems({}, "no_change")
    assert "no_change evidence requires a reason" in no_change
    assert worker._worker_verification_contract_problems({}, "review_verdict") == ["cg"]
    assert worker._worker_verification_contract_problems({}, "unknown") == [
        "unsupported verification.evidence_type: unknown"
    ]


def test_operator_result_verification_substance_paths() -> None:
    assert worker._worker_verification_contract_problems(
        {"artifacts": [{"uri": "x"}]}, "operator_result"
    ) == []
    assert worker._worker_verification_contract_problems(
        {"findings": [{"summary": "x"}]}, "operator_result"
    ) == []
    assert "requires summary" in worker._worker_verification_contract_problems(
        {}, "operator_result"
    )[0]
    assert "not substantive" in worker._worker_verification_contract_problems(
        {"summary": "hello hello hello"}, "operator_result"
    )[0]
    assert worker._worker_verification_contract_problems(
        {"summary": "Analyzed the rollout failures and documented three concrete fixes."},
        "operator_result",
    ) == []


def test_executor_verification_manifest_shapes(tmp_path) -> None:
    path = tmp_path / "executor-evidence.json"
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path) == {}
    path.write_text("not-json")
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path) == {}
    path.write_text("[]")
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path) == {}
    path.write_text(json.dumps({"metadata": {"verification": {"repo": {"head_sha": "a"}}}}))
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path)["repo"] == {
        "head_sha": "a"
    }
    path.write_text(json.dumps({"verification": {"repo": {"head_sha": "b"}}}))
    assert worker._executor_verification_manifest_from_review_workspace(tmp_path)["repo"] == {
        "head_sha": "b"
    }


def test_review_verdict_compares_executor_changed_files(monkeypatch, tmp_path) -> None:
    assert worker._worker_review_verdict_executor_repo_problems(tmp_path, {}) == []
    (tmp_path / "executor-evidence.json").write_text(
        json.dumps({"verification": {"repo": {"files_changed": ["./src//a.py", "b.py"]}}})
    )
    monkeypatch.setattr(worker, "codegraph_audit_manifest_problems", lambda manifest: [manifest["repo"]["files_changed"]])
    problems = worker._worker_review_verdict_executor_repo_problems(
        tmp_path, {"repo": {"files_changed": ["other.py"]}}
    )
    assert "must match executor evidence" in problems[0]
    assert problems[1] == ["src/a.py", "b.py"]


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ([{"status": "pass"}], True),
        ("invalid", False),
        ({"returncode": 0}, True),
        ({"returncode": "bad"}, False),
        ({"failed": 1, "status": "pass"}, False),
        ({"result": "successful"}, True),
        ({"passed": True}, True),
        ({"success": 2, "failed": 0}, True),
        ({"ok": True, "satisfied": True}, True),
        ({"nested": {"outcome": "ok"}}, True),
        ({"status": "fail"}, False),
    ],
)
def test_worker_verification_item_passed_shapes(item, expected: bool) -> None:
    assert worker._worker_verification_item_passed(item) is expected


def test_worker_repo_anchor_and_empty_change_exceptions(monkeypatch) -> None:
    monkeypatch.setattr(worker, "codegraph_audit_manifest_problems", lambda _manifest: [])
    missing = worker._worker_require_pushed_repo_anchor({})
    assert missing == ["repo evidence requires verification.repo object"]
    malformed = worker._worker_require_pushed_repo_anchor(
        {"repo": {"head_sha": "short", "dirty": True, "pushed": False}}
    )
    assert len(malformed) == 3
    assert worker._worker_require_pushed_repo_anchor(
        {"repo": {"head_sha": "a" * 40, "dirty": "0", "pr_url": "https://pr"}}
    ) == []
    assert worker._worker_allows_empty_repo_change_evidence({}, "documentation") is False
    assert worker._worker_allows_empty_repo_change_evidence({"metadata": []}, "repo_change") is False
    assert worker._worker_allows_empty_repo_change_evidence(
        {"metadata": {"origin": {"type": "beads_source_remediation"}}}, "repo_change"
    ) is True
    assert worker._worker_allows_empty_repo_change_evidence(
        {"metadata": {"remediation": {"type": "beads_source_refresh"}}}, "repo_change"
    ) is True


def test_path_and_bound_normalizers() -> None:
    assert worker._normalize_restart_services(None) == []
    assert worker._normalize_restart_services(["a.service", "", "a.service", "b-agent.service"]) == [
        "a.service",
        "b-agent.service",
    ]
    with pytest.raises(ValueError, match="invalid"):
        worker._normalize_restart_services("-bad.service")
    assert worker._bounded_int("bad", 1, 10, 5) == 5
    assert worker._bounded_int(99, 1, 10, 5) == 10
    assert worker._bounded_float("bad", 1.0, 10.0, 5.0) == 5.0
    assert worker._bounded_float(-1, 1.0, 10.0, 5.0) == 1.0
    assert worker._manifest_list(None) == []
    assert worker._manifest_list("x") == ["x"]
    assert worker._metadata_path_list("./src//a.py") == ["src/a.py"]
    assert worker._metadata_path_list(42) == []
    assert worker._nested_dict({"a": []}, "a", "b") == {}
    assert worker._repo_path_satisfies_requirement("src/a.py", "src/*.py") is True
    assert worker._repo_path_satisfies_requirement("", "src/*.py") is False


def test_repository_head_push_checks_remote_url_origin_and_branch(monkeypatch, tmp_path) -> None:
    assert worker._repository_context_head_is_pushed(tmp_path, {}) is False
    head = "a" * 40
    calls = []

    def run_git(_repo, args):
        calls.append(args)
        if args[:2] == ["ls-remote", "https://repo"]:
            return _completed(0, "%s\trefs/heads/task\n" % head)
        return _completed(1)

    monkeypatch.setattr(worker, "_run_git", run_git)
    assert worker._repository_context_head_is_pushed(
        tmp_path,
        {"head_sha": head, "remote_url": "https://repo", "remote_ref": "refs/heads/task"},
    ) is True

    monkeypatch.setattr(
        worker,
        "_run_git",
        lambda _repo, args: _completed(0, head + "\n") if args[0] == "rev-parse" else _completed(1),
    )
    assert worker._repository_context_head_is_pushed(
        tmp_path, {"head_sha": head, "remote_ref": "refs/heads/task"}
    ) is True


def test_restart_systemd_service_result_matrix(monkeypatch) -> None:
    assert worker._restart_systemd_service("../bad") ["status"] == "error"
    assert worker._restart_systemd_service("mac-agent.service")["status"] == "skipped"
    monkeypatch.setattr(worker.shutil, "which", lambda _name: None)
    assert worker._restart_systemd_service("demo.service")["reason"] == "systemctl not found"

    monkeypatch.setattr(worker.shutil, "which", lambda _name: "/bin/systemctl")
    monkeypatch.setenv("MAC_SELF_UPDATE_SERVICE_TIMEOUT", "bad")
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("inspect failed")),
    )
    assert worker._restart_systemd_service("demo.service")["error"] == "inspect failed"

    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: _completed(4, "", "denied"))
    assert worker._restart_systemd_service("demo.service")["returncode"] == 4
    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: _completed(0, "not-found\n"))
    assert worker._restart_systemd_service("demo.service")["reason"] == "service not installed"

    calls = []

    def run_success(argv, **_kwargs):
        calls.append(argv)
        return _completed(0, "loaded\n" if "show" in argv else "restarted")

    monkeypatch.setattr(worker.subprocess, "run", run_success)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 0)
    result = worker._restart_systemd_service("demo.service")
    assert result["status"] == "restarted"
    assert result["command"] == ["systemctl", "restart", "demo.service"]

    def run_restart_error(argv, **_kwargs):
        if "show" in argv:
            return _completed(0, "loaded\n")
        raise OSError("restart failed")

    monkeypatch.setattr(worker.subprocess, "run", run_restart_error)
    monkeypatch.setattr(worker.os, "geteuid", lambda: 1000)
    result = worker._restart_systemd_service("demo.service")
    assert result["status"] == "error"
    assert result["command"][:3] == ["sudo", "-n", "systemctl"]


def test_run_git_timeout_fallbacks(monkeypatch, tmp_path) -> None:
    calls = []
    monkeypatch.setenv("MAC_SELF_UPDATE_GIT_TIMEOUT", "bad")
    monkeypatch.setattr(worker.subprocess, "run", lambda argv, **kwargs: calls.append((argv, kwargs)) or _completed())
    worker._run_git(tmp_path, ["status"])
    worker._run_git_in(tmp_path, ["clone", "x"])
    assert calls[0][1]["timeout"] == 120.0
    assert calls[1][1]["timeout"] == 120.0
