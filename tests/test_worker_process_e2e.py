"""Black-box hub/worker E2E through HTTP and process entry points only."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

from mac.test_support import ephemeral_dsn

import pytest


pytestmark = pytest.mark.process_e2e


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_request(
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: Optional[Dict[str, Any]] = None,
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        base_url + path,
        data=data,
        method=method,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read()
    return json.loads(body.decode("utf-8")) if body else None


def _wait_for_hub(base_url: str, process: subprocess.Popen[str]) -> None:
    deadline = time.monotonic() + 15
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        try:
            with urllib.request.urlopen(base_url + "/health", timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, urllib.error.URLError) as exc:
            last_error = str(exc)
        time.sleep(0.1)
    stdout, stderr = process.communicate(timeout=2) if process.poll() is not None else ("", "")
    raise AssertionError(
        "mac hub did not become healthy; last_error=%r stdout=%r stderr=%r"
        % (last_error, stdout[-2000:], stderr[-2000:])
    )


def _run_mac_agent(argv: list[str], env: Dict[str, str]) -> Any:
    completed = subprocess.run(
        argv,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr + completed.stdout
    return json.loads(completed.stdout)


def test_real_hub_and_mac_agent_process_obeys_authoritative_assignment(
    tmp_path: Path,
):
    root = Path(__file__).resolve().parents[1]
    # Prefer the installed console script; fall back to `python -m mac.worker`
    # (the same `mac.worker:main` entry point) so the E2E runs in environments
    # without an editable install on PATH. worker_env carries PYTHONPATH=src below.
    mac_agent_bin = shutil.which("mac-agent")
    mac_agent = [mac_agent_bin] if mac_agent_bin else [sys.executable, "-m", "mac.worker"]

    token = "worker-process-e2e-token-with-enough-entropy"
    port = _free_port()
    base_url = "http://127.0.0.1:%d" % port
    hub_env = os.environ.copy()
    hub_env.update(
        {
            "PYTHONPATH": str(root / "src")
            + os.pathsep
            + hub_env.get("PYTHONPATH", ""),
            # A real DSN, not a file: the hub subprocess opens the control-plane
            # authority itself, and SQLite is no longer a supported backend.
            "MAC_DB": ephemeral_dsn(),
            "MAC_API_TOKEN": token,
            "MAC_SECRET_KEY": "worker-process-e2e-secret-key-with-32-plus-chars",
            "MAC_RECORD_HTTP_OBSERVATIONS": "1",
            "HERMES_HOME": str(tmp_path / ".hermes"),
            "ACC_DIR": str(tmp_path / ".acc"),
        }
    )
    hub = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "mac.api:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "1",
            "--log-level",
            "warning",
        ],
        cwd=str(root),
        env=hub_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_hub(base_url, hub)

        _json_request(
            base_url,
            token,
            "POST",
            "/projects",
            {"name": "mac-canary"},
        )
        normal = _json_request(
            base_url,
            token,
            "POST",
            "/tasks",
            {
                "title": "normal task must not run",
                "project": "mac-canary",
                "priority": 100,
                "required_capabilities": ["python"],
            },
        )
        canary = _json_request(
            base_url,
            token,
            "POST",
            "/tasks",
            {
                "title": "canary task may run",
                "project": "mac-canary",
                "priority": 10,
                "required_capabilities": ["python"],
                "metadata": {"canary": True},
            },
        )

        worker_env = hub_env.copy()
        # This process explicitly owns an attestation-key file. Ignore any
        # ambient key inherited from an in-process CLI test or operator shell;
        # otherwise a re-registration can sign with the wrong agent key.
        worker_env.pop("MAC_ATTESTATION_KEY", None)
        workspace = tmp_path / "worker-workspaces"
        common = [
            *mac_agent,
            "--url",
            base_url,
            "--token",
            token,
            "--register",
            "--agent-name",
            "process-e2e-worker",
            "--hostname",
            "process-e2e-host",
            "--capabilities",
            "python",
            "--workspace",
            str(workspace),
            "--attestation-key-env",
            str(tmp_path / "attestation.env"),
            "--allowed-projects",
            "mac-canary",
            "--claim-only-canary-tasks",
            "--poll-interval",
            "0",
        ]

        dry_run = _run_mac_agent(common + ["--dry-run-claim"], worker_env)
        assert dry_run["status"] == "dry_run"
        # Worker-local canary/project switches are compatibility inputs only.
        # The hub's global priority order is the authoritative assignment.
        assert dry_run["assignment"]["task"]["id"] == normal["id"]
        assert dry_run["assignment"]["lease"] is None

        after_dry_run = {
            task["id"]: task
            for task in _json_request(base_url, token, "GET", "/tasks")
        }
        assert after_dry_run[normal["id"]]["state"] == "open"
        assert after_dry_run[canary["id"]]["state"] == "open"
        assert after_dry_run[canary["id"]]["lease_id"] is None

        executor = tmp_path / "executor.py"
        executor.write_text(
            """
from __future__ import annotations

import json
import os
from pathlib import Path

task_file = Path(os.environ["MAC_TASK_FILE"])
workspace = Path(os.environ["MAC_TASK_WORKSPACE"])
envelope = json.loads(task_file.read_text(encoding="utf-8"))
task = envelope["task"]
assert envelope["lease"]["task_id"] == task["id"]
assert os.environ["MAC_TASK_ID"] == task["id"]
(workspace / "executor-ran.json").write_text(
    json.dumps({"task_id": task["id"], "title": task["title"]}, sort_keys=True),
    encoding="utf-8",
)
(workspace / "mac-evidence.json").write_text(
    json.dumps(
        {
            "schema": "mac.worker_evidence.v1",
            "status": "complete",
            "evidence_type": "test",
            "repo": {
                "head_sha": "abcdef1234567890abcdef1234567890abcdef12",
                "pushed": True,
                "remote_ref": "refs/heads/task/process-e2e",
                "dirty": False,
            },
            "checks": [{"name": "executor", "returncode": 0}],
        },
        sort_keys=True,
    ),
    encoding="utf-8",
)
print("executor completed " + task["id"])
""".lstrip(),
            encoding="utf-8",
        )

        run = _run_mac_agent(
            common
            + [
                "--loop",
                "--max-iterations",
                "1",
                "--executor",
                sys.executable,
                str(executor),
            ],
            worker_env,
        )
        assert len(run) == 1
        assert run[0]["status"] == "submitted_for_review", run
        assert run[0]["task"]["id"] == normal["id"]

        final_tasks = {
            task["id"]: task
            for task in _json_request(base_url, token, "GET", "/tasks")
        }
        assert final_tasks[normal["id"]]["state"] in {"needs_review", "reviewing"}
        assert final_tasks[canary["id"]]["state"] == "open"
        assert final_tasks[canary["id"]]["lease_id"] is None

        detail = _json_request(base_url, token, "GET", "/tasks/%s" % normal["id"])
        assert detail["evidence"][0]["summary"].startswith("executor completed ")
        assert detail["evidence"][0]["metadata"]["returncode"] == 0

        observations = _json_request(
            base_url,
            token,
            "GET",
            "/observability/logs?limit=100",
        )
        names = {item["name"] for item in observations}
        assert {
            "worker.routing.policy",
            "worker.routing.dry_run_result",
            "worker.task_claimed",
            "worker.execution.completed",
        } <= names
    finally:
        if hub.poll() is None:
            hub.terminate()
            try:
                hub.wait(timeout=5)
            except subprocess.TimeoutExpired:
                hub.kill()
                hub.wait(timeout=5)
