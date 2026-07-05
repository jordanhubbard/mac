"""Coverage for worker install, artifact, Slack, and repository helpers."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import worker


class _Client:
    def __init__(self) -> None:
        self.posts = []
        self.get_value = {}
        self.fail = False

    def post(self, path, payload):
        if self.fail:
            raise RuntimeError("offline")
        self.posts.append((path, payload))
        return {}

    def get(self, _path):
        if self.fail:
            raise RuntimeError("offline")
        return self.get_value


def _worker(tmp_path, client=None):
    return worker.MacWorker(
        client or _Client(),
        "agent",
        tmp_path,
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


def test_load_verification_manifest_missing_invalid_and_valid(tmp_path) -> None:
    instance = _worker(tmp_path)
    assert instance._load_verification_manifest(tmp_path)["status"] == "missing"
    path = tmp_path / "mac-evidence.json"
    path.write_text("not-json")
    assert instance._load_verification_manifest(tmp_path)["status"] == "invalid"
    path.write_text("[]")
    assert "JSON object" in instance._load_verification_manifest(tmp_path)["problems"][0]
    path.write_text('{"status":"complete"}')
    manifest = instance._load_verification_manifest(tmp_path)
    assert manifest["schema"] == "mac.worker_evidence.v1"
    assert manifest["sha256"].startswith("sha256:")


def test_command_audit_and_footprint_reporting_are_best_effort(tmp_path) -> None:
    client = _Client()
    instance = _worker(tmp_path, client)
    instance._record_command_audit({"command_id": "c", "metadata": {}})
    assert client.posts[-1][0].endswith("/command-audit")
    instance._report_footprint({"pip": []})
    assert client.posts[-1][0].endswith("/installed-packages")
    client.fail = True
    instance._record_command_audit({})
    instance._report_footprint({})


def test_pip_and_npm_inventory_parsing_and_failures(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, '[{"name":"My_Pkg","version":"1.2"}]', ""
        ),
    )
    assert instance._pip_installed("python") == {"my-pkg": "1.2"}
    assert instance._pip_spec_satisfied("my-pkg", {"my-pkg": "1.2"}) is True
    assert instance._pip_spec_satisfied("my-pkg==1.2", {"my-pkg": "1.2"}) is True
    assert instance._pip_spec_satisfied("missing", {}) is False
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess(
            [], 0, '{"dependencies":{"One":{},"@scope/pkg":{}}}', ""
        ),
    )
    assert instance._npm_installed(str(tmp_path)) == {"one", "@scope/pkg"}
    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("bad")))
    assert instance._pip_installed("python") == {}
    assert instance._npm_installed(str(tmp_path)) == set()


def test_run_install_success_failure_and_exception(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    audits = []
    monkeypatch.setattr(instance, "_record_command_audit", audits.append)
    monkeypatch.setattr(
        worker.subprocess,
        "run",
        lambda *_a, **_k: subprocess.CompletedProcess([], 3, "out", "err"),
    )
    result = instance._run_install(["tool"], manager="pip", reason="test", specs=["one"])
    assert result["ok"] is False and result["returncode"] == 3
    assert audits[-1]["phase"] == "failed"
    monkeypatch.setattr(worker.subprocess, "run", lambda *_a, **_k: (_ for _ in ()).throw(OSError("missing")))
    result = instance._run_install(["tool"], manager="pip", reason="test", specs=["one"])
    assert result == {"ok": False, "error": "missing", "specs": ["one"]}


def test_ensure_package_paths_and_footprint(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    assert instance.ensure_pip(["--bad"]) == {"ok": True, "skipped": "no specs"}
    assert instance.ensure_npm(["--bad"]) == {"ok": True, "skipped": "no packages"}
    updates = []
    monkeypatch.setattr(instance, "_update_footprint", lambda *args, **kwargs: updates.append((args, kwargs)))
    monkeypatch.setattr(instance, "_pip_installed", lambda _py: {"one": "1.0"})
    assert instance.ensure_pip(["one"])["skipped"] == "already satisfied"
    monkeypatch.setattr(instance, "_npm_installed", lambda _prefix: {"one"})
    assert instance.ensure_npm(["one@2"])["skipped"] == "already satisfied"

    lock = SimpleNamespace(close=lambda: updates.append(("closed", {})))
    monkeypatch.setattr(instance, "_install_lock", lambda: lock)
    monkeypatch.setattr(instance, "_npm_installed", lambda _prefix: set())
    monkeypatch.setattr(instance, "_run_install", lambda *args, **kwargs: {"ok": True})
    assert instance.ensure_npm(["two@1"])["ok"] is True
    assert any(item[0] == "closed" for item in updates)


def test_reconcile_runtime_dependencies_best_effort(monkeypatch, tmp_path) -> None:
    instance = _worker(tmp_path)
    monkeypatch.setenv("MAC_AGENT_RECONCILE_RUNTIME_DEPS", "off")
    instance._reconcile_runtime_deps_best_effort()
    logs = []
    monkeypatch.setenv("MAC_AGENT_RECONCILE_RUNTIME_DEPS", "1")
    monkeypatch.setattr(instance, "_observe_log", lambda *args, **kwargs: logs.append((args, kwargs)))
    monkeypatch.setattr(instance, "reconcile_runtime_deps", lambda: {"ok": True, "skipped": "done"})
    instance._reconcile_runtime_deps_best_effort()
    monkeypatch.setattr(instance, "reconcile_runtime_deps", lambda: (_ for _ in ()).throw(RuntimeError("bad")))
    instance._reconcile_runtime_deps_best_effort()
    assert logs[0][0][0] == "worker.runtime_deps.reconciled"
    assert logs[-1][0][0] == "worker.runtime_deps.error"


def test_service_claim_sync_failures_and_success(monkeypatch, tmp_path) -> None:
    client = _Client()
    instance = _worker(tmp_path, client)
    client.fail = True
    instance._sync_service_claims()
    client.fail = False
    client.get_value = {"resources": {"hardware": {}}}
    monkeypatch.setattr(worker, "_build_willing_media_routes", lambda *_a, **_k: [])
    instance._sync_service_claims()
    monkeypatch.setattr(
        worker,
        "_build_willing_media_routes",
        lambda *_a, **_k: [{"op": "image"}, {"op": "video"}],
    )

    def post(path, payload):
        client.posts.append((path, payload))
        if path.endswith("service-claims/sync"):
            return {"held": ["image"], "managed": ["image"]}
        return {}

    client.post = post
    instance._sync_service_claims()
    heartbeat = client.posts[-1][1]
    assert {item["op"] for item in heartbeat["resources"]["media_routes"]} == {
        "image",
        "video",
    }


def test_json_slack_and_output_helpers(tmp_path) -> None:
    assert worker._load_json_file(tmp_path / "missing") is None
    (tmp_path / "slack_accounts.json").write_text(
        json.dumps({"accounts": {"workspace": {"team_id": "T"}, "bad": "x"}})
    )
    accounts = worker._load_slack_accounts(tmp_path)
    assert accounts[0]["name"] == "workspace"
    (tmp_path / "slack_home_channels.json").write_text(
        json.dumps({"home_channels": {"home": {"channel_id": "C"}}})
    )
    assert worker._load_slack_home_channels(tmp_path)[0]["name"] == "home"
    assert worker._target_slack_route({"external_id": "T/C"}) == ("T", "C")
    assert worker._status_update_slack_text({"event_type": "task", "body": "done"}) == "[task] done"
    assert worker._status_update_slack_text({"title": "title", "body": "title"}) == "title"
    assert worker._coerce_process_output(None) == ""
    assert "�" in worker._coerce_process_output(b"\xff")
    safe = worker._audit_safe_argv(["cmd", "--password", "secret", "token=value", "x" * 513])
    assert "secret" not in " ".join(safe)
    assert safe[-1].startswith("<truncated:")
    assert worker._summary_from_output(0, "", "") == "executor completed"
    assert "returncode 2" in worker._summary_from_output(2, "", "")


def test_artifact_capture_limits_types_and_deduplication(monkeypatch, tmp_path) -> None:
    assert worker._sha256_file(tmp_path / "missing") == ""
    monkeypatch.setenv("MAC_EVIDENCE_ARTIFACT_MAX_BYTES", "bad")
    monkeypatch.setenv("MAC_EVIDENCE_ARTIFACT_TOTAL_MAX_BYTES", "bad")
    assert worker._evidence_artifact_max_bytes() == 5 * 1024 * 1024
    assert worker._evidence_artifact_total_max_bytes() == 50 * 1024 * 1024
    assert worker._artifact_content_type(Path("a.json")) == "application/json"
    assert worker._artifact_content_type(Path("a.md")).startswith("text/plain")
    assert worker._artifact_content_type(Path("a.bin")) == "application/octet-stream"
    source = tmp_path / "stdout.txt"
    source.write_bytes(b"abcdef")
    captured = worker._capture_evidence_artifact(
        source, name="stdout", artifact_type="stdout", max_bytes=3
    )
    assert captured["size_bytes"] == 3
    assert captured["truncated"] is True
    assert worker._capture_evidence_artifact(
        tmp_path / "missing", name="x", artifact_type="x", max_bytes=1
    ) is None
    artifacts = worker._durable_evidence_artifacts(tmp_path, source)
    assert len([item for item in artifacts if item["source_uri"] == source.resolve().as_uri()]) == 1


def test_repository_origin_remote_and_changed_file_helpers(monkeypatch, tmp_path) -> None:
    assert worker._repository_task_origin({}) is None
    assert worker._repository_task_origin(
        {"metadata": {"origin": {"repository_url": "https://repo", "type": "direct_task"}}}
    )["repository_url"] == "https://repo"
    assert worker._repository_task_origin(
        {
            "metadata": {
                "origin": {"repository_url": "https://repo", "type": "direct_task"},
                "remediation": {"type": "beads_source_refresh"},
            }
        }
    ) is None
    task = {"metadata": {"origin": {"repository_url": "https://origin"}}}
    assert worker._repository_publication_remote(task) == "https://origin"
    assert worker._repository_publication_remote({}, {"repository_canonical_remote_url": "https://context"}) == "https://context"
    assert worker._repository_publication_remote({}) == ""
    assert worker._remote_branch_from_ref("refs/heads/main") == "main"
    assert worker._remote_branch_from_ref("refs/tags/v1") == ""

    results = iter(
        [
            subprocess.CompletedProcess([], 1, "", ""),
            subprocess.CompletedProcess([], 0, "a.py\n", ""),
            subprocess.CompletedProcess([], 0, "b.py\n", ""),
            subprocess.CompletedProcess([], 0, "c.py\n", ""),
        ]
    )
    monkeypatch.setattr(worker, "_run_git", lambda *_a, **_k: next(results))
    changed = worker._repository_context_changed_files(
        tmp_path, {"repository_base_sha": "a" * 40}
    )
    assert changed == ["a.py", "b.py", "c.py"]


def test_codegraph_audit_attachment_and_checks(monkeypatch, tmp_path) -> None:
    manifest = {"checks": [{"name": "codegraph_audit", "status": "old"}]}
    worker._append_codegraph_audit_check(manifest, {"status": "skipped"})
    assert manifest["checks"][0]["status"] == "old"
    monkeypatch.setattr(worker, "codegraph_audit_check", lambda audit: {"name": "codegraph_audit", **audit})
    worker._append_codegraph_audit_check(manifest, {"status": "pass"})
    assert manifest["checks"] == [{"name": "codegraph_audit", "status": "pass"}]
    assert worker._attach_repository_codegraph_audit({}, {}) == {}
    context = {"repository_worktree": str(tmp_path)}
    monkeypatch.setattr(worker, "_repository_context_changed_files", lambda *_a: ["a.py"])
    monkeypatch.setattr(worker, "codegraph_audit_manifest_problems", lambda _m: ["missing"])
    monkeypatch.setattr(worker, "run_codegraph_audit", lambda *_a: {"status": "pass"})
    attached = worker._attach_repository_codegraph_audit({}, context)
    assert attached["repo"]["files_changed"] == ["a.py"]
    assert attached["codegraph"]["status"] == "pass"


def test_repository_contract_and_sandbox_verification_helpers(tmp_path) -> None:
    task = {
        "metadata": {
            "execution_contract": {
                "test": {"command": "make test"},
                "repository_contract": {"canonical_remote_url": "https://canonical"},
            }
        }
    }
    assert worker._repository_contract_test_command(task) == "make test"
    assert worker._repository_contract_canonical_remote(task) == "https://canonical"
    assert worker._repository_contract_test_command({"metadata": []}) == ""
    assert worker._sandbox_repository_verification_item(None, "test") is None
    path = tmp_path / "mac-sandbox-verification.json"
    path.write_text("[]")
    assert worker._sandbox_repository_verification_item(tmp_path, "test") is None
    path.write_text(
        json.dumps(
            {
                "returncode": "bad",
                "command": "make test",
                "stdout": "out",
                "environment_delta": {"added": ["x"]},
                "worktree": "/work",
            }
        )
    )
    item = worker._sandbox_repository_verification_item(tmp_path, "test")
    assert item["returncode"] == 1
    assert item["execution_environment"] == "openshell_sandbox"


def test_required_changed_file_collection_and_problems() -> None:
    task = {
        "metadata": {
            "required_changed_files": ["src/*.py"],
            "execution_contract": {"evidence": {"required_files": ["README.md"]}},
        }
    }
    required = worker._required_changed_files_from_task(task)
    assert "src/*.py" in required
    problems = worker._worker_required_changed_file_problems(
        task, {"repo": {"files_changed": ["src/a.py"]}}
    )
    assert "README.md" in problems[0]


def test_truncate_process_text_keeps_the_failure_tail() -> None:
    # pytest prints its failure summary LAST; a head-only cut made every long
    # verification failure undiagnosable from the ledger (observed live).
    from mac.worker import _truncate_process_text

    text = ("x" * 10000) + "\nFAILED tests/test_thing.py::test_case - boom\n1 failed"
    out = _truncate_process_text(text, limit=4000)
    assert len(out) < 4200  # bounded (marker adds a few chars)
    assert out.startswith("x")                      # head kept for context
    assert "chars omitted" in out                   # explicit gap marker
    assert "FAILED tests/test_thing.py" in out      # the diagnosis survives
    assert out.endswith("1 failed")
    # Short output passes through untouched.
    assert _truncate_process_text("all good", limit=4000) == "all good"


# ---------------------------------------------------------------------------
# Tests for hub-verify deferred mode
# ---------------------------------------------------------------------------


def test_hub_verify_deferred_item_sentinel() -> None:
    """_hub_verify_deferred_test_item returns the expected sentinel shape."""
    item = worker._hub_verify_deferred_test_item("scripts/run-contract-tests.sh")
    assert item["status"] == "deferred"
    assert item["execution_environment"] == "hub_verify_pending"
    assert item["returncode"] is None
    assert item["name"] == "repository contract test"
    assert item["command"] == "scripts/run-contract-tests.sh"


def test_is_hub_verify_deferred_item_recognizes_sentinel() -> None:
    sentinel = worker._hub_verify_deferred_test_item("cmd")
    assert worker._is_hub_verify_deferred_item(sentinel) is True


def test_is_hub_verify_deferred_item_rejects_non_deferred() -> None:
    passing = {"name": "t", "returncode": 0, "status": "pass"}
    failing = {"name": "t", "returncode": 1, "status": "fail"}
    assert worker._is_hub_verify_deferred_item(passing) is False
    assert worker._is_hub_verify_deferred_item(failing) is False
    assert worker._is_hub_verify_deferred_item(None) is False  # type: ignore[arg-type]
    assert worker._is_hub_verify_deferred_item("not a dict") is False  # type: ignore[arg-type]


def test_sandbox_verification_item_returns_deferred_when_hub_verify_no_task_dir() -> None:
    """When task_dir=None and hub_verify=True, return the deferred sentinel."""
    item = worker._sandbox_repository_verification_item(None, "cmd", hub_verify=True)
    assert item is not None
    assert worker._is_hub_verify_deferred_item(item)


def test_sandbox_verification_item_returns_none_without_hub_verify_no_task_dir() -> None:
    """Option A fallback: hub_verify=False keeps original None-return behaviour."""
    item = worker._sandbox_repository_verification_item(None, "cmd", hub_verify=False)
    assert item is None


def test_sandbox_verification_item_returns_deferred_when_hub_verify_missing_file(tmp_path) -> None:
    """No mac-sandbox-verification.json + hub_verify=True -> deferred sentinel."""
    item = worker._sandbox_repository_verification_item(tmp_path, "cmd", hub_verify=True)
    assert item is not None
    assert worker._is_hub_verify_deferred_item(item)


def test_sandbox_verification_item_returns_none_when_no_hub_verify_missing_file(tmp_path) -> None:
    """No mac-sandbox-verification.json + hub_verify=False -> None (original behaviour)."""
    item = worker._sandbox_repository_verification_item(tmp_path, "cmd", hub_verify=False)
    assert item is None


def test_sandbox_verification_item_returns_real_result_when_file_present_hub_verify(tmp_path) -> None:
    """A present sandbox-verification file takes precedence over deferred mode."""
    path = tmp_path / "mac-sandbox-verification.json"
    path.write_text(json.dumps({"returncode": 0, "command": "make test", "stdout": "ok", "stderr": ""}))
    item = worker._sandbox_repository_verification_item(tmp_path, "cmd", hub_verify=True)
    assert item is not None
    assert item["status"] == "pass"
    assert item["returncode"] == 0
    assert item["execution_environment"] == "openshell_sandbox"


def test_prepush_problems_skips_test_gate_for_deferred_item() -> None:
    """hub_verify=True + deferred test_item: test gate is skipped."""
    task: dict = {}
    repo = {
        "head_sha": "a" * 40,
        "dirty": False,
        "files_changed": ["src/mac/worker.py"],
    }
    deferred = worker._hub_verify_deferred_test_item("scripts/run-contract-tests.sh")
    problems = worker._repository_finalizer_prepush_problems(
        task, repo, deferred, hub_verify=True
    )
    assert problems == []


def test_prepush_problems_enforces_other_checks_in_deferred_mode() -> None:
    """hub_verify deferred mode: non-test checks (dirty, head_sha) are still enforced."""
    task: dict = {}
    repo = {
        "head_sha": "not-a-sha",
        "dirty": True,
        "files_changed": [],
    }
    deferred = worker._hub_verify_deferred_test_item("cmd")
    problems = worker._repository_finalizer_prepush_problems(
        task, repo, deferred, hub_verify=True
    )
    assert any("head_sha" in p for p in problems)
    assert any("dirty" in p for p in problems)
    assert any("changed files" in p for p in problems)
    # test-gate problem must NOT be present
    assert not any("passing test" in p for p in problems)


def test_prepush_problems_fallback_option_a_fails_without_passing_test() -> None:
    """Option A (hub_verify=False): missing passing test still blocks push."""
    task: dict = {}
    repo = {
        "head_sha": "b" * 40,
        "dirty": False,
        "files_changed": ["src/mac/worker.py"],
    }
    failing = {"name": "t", "returncode": 1, "status": "fail"}
    problems = worker._repository_finalizer_prepush_problems(
        task, repo, failing, hub_verify=False
    )
    assert any("passing test" in p for p in problems)


def test_prepush_problems_fallback_option_a_passes_with_passing_test() -> None:
    """Option A (hub_verify=False): a passing test_item produces no problems."""
    task: dict = {}
    repo = {
        "head_sha": "c" * 40,
        "dirty": False,
        "files_changed": ["src/mac/worker.py"],
    }
    passing = {"name": "t", "returncode": 0, "status": "pass"}
    problems = worker._repository_finalizer_prepush_problems(
        task, repo, passing, hub_verify=False
    )
    assert problems == []


def test_sandbox_verification_item_hub_verify_invalid_json_returns_deferred(tmp_path) -> None:
    """Invalid JSON in sandbox file + hub_verify=True -> deferred sentinel."""
    path = tmp_path / "mac-sandbox-verification.json"
    path.write_text("not-json")
    item = worker._sandbox_repository_verification_item(tmp_path, "cmd", hub_verify=True)
    assert item is not None
    assert worker._is_hub_verify_deferred_item(item)


def test_sandbox_verification_item_hub_verify_non_dict_json_returns_deferred(tmp_path) -> None:
    """Non-dict JSON (list) in sandbox file + hub_verify=True -> deferred sentinel."""
    path = tmp_path / "mac-sandbox-verification.json"
    path.write_text("[]")
    item = worker._sandbox_repository_verification_item(tmp_path, "cmd", hub_verify=True)
    assert item is not None
    assert worker._is_hub_verify_deferred_item(item)

