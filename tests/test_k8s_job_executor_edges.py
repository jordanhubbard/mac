from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from mac.k8s import job_executor


class _Mac:
    def __init__(self, *, fail: str | None = None, evidence: Any = None) -> None:
        self.fail = fail
        self.evidence = {"id": "ev-1"} if evidence is None else evidence
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def get(self, path: str) -> dict[str, str]:
        if self.fail == "get":
            raise RuntimeError("get failed")
        return {"id": "task/1", "title": "edge task"}

    def post(self, path: str, body: dict[str, Any]) -> Any:
        self.posts.append((path, body))
        if self.fail == "start" and "/start?" in path:
            raise RuntimeError("permission denied")
        if self.fail == "evidence" and path.endswith("/evidence"):
            raise RuntimeError("evidence down")
        if self.fail == "tick" and path.startswith("/reviews/default/tick"):
            raise RuntimeError("tick down")
        if self.fail == "transition" and path.endswith("/transition"):
            raise RuntimeError("transition down")
        if path.endswith("/evidence"):
            return self.evidence
        return {"ok": True}


def _env(**updates: str) -> dict[str, str]:
    env = {
        "MAC_TASK_ID": "task/1",
        "MAC_LEASE_ID": "lease-1",
        "MAC_AGENT_ID": "agent/1",
        "MAC_URL": "http://mac",
        "MAC_WORKER_TOKEN": "token",
    }
    env.update(updates)
    return env


def _ok(_task: dict[str, Any]) -> job_executor._ExecResult:
    return job_executor._ExecResult(returncode=0, stdout="ok", stdout_sha256="sum")


def test_resolve_constructs_default_client_and_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel_mac = object()
    sentinel_executor = object()
    monkeypatch.setattr(
        job_executor, "_default_mac_client", lambda url, token: (sentinel_mac, url, token)
    )
    monkeypatch.setattr(
        job_executor, "_default_subprocess_executor", lambda env: (sentinel_executor, env)
    )

    mac, executor = job_executor._resolve_mac_and_executor(
        {"MAC_HUB_URL": "http://hub", "MAC_API_TOKEN": "api"}, None, None
    )

    assert mac == (sentinel_mac, "http://hub", "api")
    assert executor[0] is sentinel_executor


def test_start_failure_without_already_aborts_before_execution() -> None:
    result = job_executor.run_one_lease(mac=_Mac(fail="start"), executor=_ok, env=_env())
    assert result.status == "no-evidence"
    assert "start failed" in (result.error or "")


def test_review_missing_task_and_get_failure_are_reported() -> None:
    missing = job_executor._run_one_review(
        mac=_Mac(), executor=_ok, env={"MAC_REVIEW_ID": "review-1"}, sleeper=None
    )
    failed_get = job_executor.run_one_lease(
        mac=_Mac(fail="get"),
        executor=_ok,
        env=_env(MAC_REVIEW_ID="review-1"),
    )
    assert missing.status == "missing-env"
    assert failed_get.status == "no-evidence"


def test_review_metadata_success_and_tick_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    mac = _Mac(fail="tick", evidence="not-a-mapping")

    def execute(_task: dict[str, Any]) -> job_executor._ExecResult:
        return job_executor._ExecResult(
            returncode=0,
            stdout="reviewed",
            stdout_sha256="digest",
            verification_manifest={"status": "complete"},
            manifest_path="/tmp/evidence.json",
            manifest_error="advisory warning",
        )

    result = job_executor.run_one_lease(
        mac=mac,
        executor=execute,
        env=_env(
            MAC_REVIEW_ID="review-1", MAC_REVIEW_TARGET_EVIDENCE_ID="target-1"
        ),
    )

    assert result.status == "submitted-for-review"
    assert result.evidence_id is None
    evidence = next(body for path, body in mac.posts if path.endswith("/evidence"))
    metadata = evidence["metadata"]
    assert metadata["verification"] == {"status": "complete"}
    assert metadata["verification_manifest_path"] == "/tmp/evidence.json"
    assert metadata["verification_manifest_error"] == "advisory warning"
    assert "post-review tick failed" in caplog.text


def test_review_evidence_failure_returns_no_evidence() -> None:
    result = job_executor.run_one_lease(
        mac=_Mac(fail="evidence"),
        executor=_ok,
        env=_env(MAC_REVIEW_ID="review-1"),
    )
    assert result.status == "no-evidence"
    assert "evidence down" in (result.error or "")


def test_unconfigured_executor_refuses_noop() -> None:
    result = job_executor._default_subprocess_executor({})({"id": "task-1"})
    assert result.returncode == 1
    assert "refusing" in result.stderr


def test_subprocess_executor_tolerates_stale_manifest_unlink_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(job_executor.os.path, "exists", lambda _path: True)

    def fail_unlink(_path: str) -> None:
        raise OSError("busy")

    monkeypatch.setattr(job_executor.os, "unlink", fail_unlink)
    monkeypatch.setattr(
        job_executor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=None, stderr=None),
    )
    monkeypatch.setattr(
        job_executor,
        "_read_verification_manifest",
        lambda path: (None, f"missing: {path}"),
    )

    result = job_executor._default_subprocess_executor(
        {
            "MAC_TASK_EXECUTOR_COMMAND": "ignored",
            "MAC_TASK_EXECUTOR_TIMEOUT_SECONDS": "9",
            "MAC_TASK_EVIDENCE_MANIFEST_PATH": "/tmp/stale.json",
        }
    )({"id": None, "title": None})

    assert result.returncode == 0
    assert result.stdout == ""
    assert result.manifest_error == "missing: /tmp/stale.json"


def test_pod_log_forwarding_truncates_and_never_raises() -> None:
    target = io.StringIO()
    job_executor._forward_to_pod_logs("large", "abcdef", stream=target, tail_bytes=3)
    assert "last 3 bytes" in target.getvalue()
    assert "def" in target.getvalue()

    class BrokenStream:
        def write(self, _value: str) -> None:
            raise RuntimeError("broken")

    job_executor._forward_to_pod_logs("broken", "text", stream=BrokenStream())


def test_manifest_os_error_is_descriptive(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("{}", encoding="utf-8")

    def deny(*args: Any, **kwargs: Any) -> Any:
        raise PermissionError("denied")

    monkeypatch.setattr("builtins.open", deny)
    manifest, error = job_executor._read_verification_manifest(str(path))
    assert manifest is None
    assert "read failed" in (error or "")


def test_default_client_passes_url_and_token(monkeypatch: pytest.MonkeyPatch) -> None:
    from mac import api_client

    monkeypatch.setattr(
        api_client, "MacApiClient", lambda url, token: {"url": url, "token": token}
    )
    assert job_executor._default_mac_client("http://mac", "secret") == {
        "url": "http://mac",
        "token": "secret",
    }


def test_executor_failure_without_evidence_and_transition_failure_are_safe() -> None:
    def crash(_task: dict[str, Any]) -> job_executor._ExecResult:
        raise RuntimeError("crash")

    no_evidence = job_executor.run_one_lease(
        mac=_Mac(fail="evidence"), executor=crash, env=_env()
    )
    blocked = job_executor.run_one_lease(
        mac=_Mac(fail="transition"),
        executor=lambda _task: job_executor._ExecResult(returncode=4, stdout="bad"),
        env=_env(),
    )
    assert no_evidence.status == "no-evidence"
    assert blocked.status == "blocked"


def test_main_prints_result_and_returns_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        job_executor,
        "run_one_lease",
        lambda: job_executor.JobExecutionResult(
            status="failed",
            task_id="task-1",
            lease_id="lease-1",
            returncode=9,
            evidence_id="ev-1",
            error="boom",
        ),
    )
    assert job_executor.main([]) == 0
    assert "status=failed" in capsys.readouterr().out


def test_resolve_prefers_fleet_scoped_worker_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_resolve_mac_and_executor must honor MAC_WORKER_TOKEN__<FLEET> from the
    passed env dict (env["MAC_FLEET"]) over the legacy flat form, and follow the
    MAC_WORKER_TOKEN > MAC_API_TOKEN chain precedence (mac-g55y)."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        job_executor,
        "_default_mac_client",
        lambda url, token: captured.update(url=url, token=token) or object(),
    )
    monkeypatch.setattr(
        job_executor, "_default_subprocess_executor", lambda env: object()
    )

    # Scoped worker token outranks both legacy flat forms.
    job_executor._resolve_mac_and_executor(
        {
            "MAC_URL": "http://mac",
            "MAC_FLEET": "rocky",
            "MAC_WORKER_TOKEN__ROCKY": "worker-rocky",
            "MAC_WORKER_TOKEN": "worker-flat",
            "MAC_API_TOKEN": "api-flat",
        },
        None,
        None,
    )
    assert captured["token"] == "worker-rocky"

    # No scoped form -> legacy chain, MAC_WORKER_TOKEN ahead of MAC_API_TOKEN.
    job_executor._resolve_mac_and_executor(
        {
            "MAC_URL": "http://mac",
            "MAC_FLEET": "rocky",
            "MAC_WORKER_TOKEN": "worker-flat",
            "MAC_API_TOKEN": "api-flat",
        },
        None,
        None,
    )
    assert captured["token"] == "worker-flat"

    # Scoped MAC_API_TOKEN__ROCKY wins when no worker token resolves.
    job_executor._resolve_mac_and_executor(
        {
            "MAC_URL": "http://mac",
            "MAC_FLEET": "rocky",
            "MAC_API_TOKEN__ROCKY": "api-rocky",
            "MAC_API_TOKEN": "api-flat",
        },
        None,
        None,
    )
    assert captured["token"] == "api-rocky"


def test_review_mode_token_read_is_fleet_scoped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The review-mode token read must prefer the fleet-scoped worker token from
    env["MAC_FLEET"] over the legacy flat form (mac-g55y)."""
    captured: dict[str, Any] = {}

    def _fake_client(url: str, token: str) -> Any:
        captured.update(url=url, token=token)
        return _Mac(fail="get")

    monkeypatch.setattr(job_executor, "_default_mac_client", _fake_client)

    job_executor._run_one_review(
        mac=None,
        executor=_ok,
        env={
            "MAC_REVIEW_ID": "review-1",
            "MAC_TASK_ID": "task/1",
            "MAC_URL": "http://mac",
            "MAC_FLEET": "rocky",
            "MAC_WORKER_TOKEN__ROCKY": "worker-rocky",
            "MAC_WORKER_TOKEN": "worker-flat",
            "MAC_API_TOKEN": "api-flat",
        },
        sleeper=None,
    )
    assert captured["token"] == "worker-rocky"
