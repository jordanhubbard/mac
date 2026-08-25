"""A worker that is being shut down must let go of its lease.

Audit 2026-08-17. The worker installed SIGTERM/SIGINT handlers that only set
``self._stop``, which is read at the TOP of ``run_forever``'s loop. Executor
children are spawned with ``start_new_session=True`` so they deliberately never
see the signal — correct, until the unit's ``TimeoutStopSec`` expires and
SIGKILL arrives. Then ``_shutdown()`` never runs (no "offline" heartbeat), the
lease-renewal thread dies with the process, and the task stays LEASED and
undispatchable for the rest of ``lease_seconds`` (900s by default) with no
evidence published — while the attempt still burns. The hub's recurring
"Agent went offline mid-task (lease expired / heartbeat lost)" diagnosis is
this, seen from the other end.

These tests pin the fix: a second signal, or a bounded grace after the first,
actively abandons — release the lease, post the offline heartbeat, and publish
a ``worker.shutdown.abandoned`` observation so the hub can distinguish
"worker restarted" from "worker crashed".
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi.testclient import TestClient

from mac.api import create_app
from mac.hermes_adapter import MacApiClient, MacApiError
from mac.models import TaskState
from mac.services import ControlPlane
from mac.worker import (
    DEFAULT_SHUTDOWN_GRACE_SECONDS,
    MacWorker,
    WorkerExecution,
)


def _api_transport(client: TestClient):
    def transport(method: str, path: str, payload: Optional[Dict[str, Any]]) -> Any:
        request = getattr(client, method.lower())
        kwargs: Dict[str, Any] = {}
        if payload is not None:
            kwargs["json"] = payload
        response = request(path, **kwargs)
        if response.status_code >= 400:
            raise MacApiError(response.text)
        return response.json() if response.content else None

    return transport


def _register_worker(cp: ControlPlane):
    machine = cp.register_machine("shutdown-host")
    return cp.register_agent(machine.id, "shutdown-worker", capabilities=["python"])


def _wait_for(predicate, timeout: float = 10.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_shutdown_grace_expiry_releases_the_lease_and_marks_agent_offline(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = _register_worker(cp)
    task = cp.create_task("Long task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    executor_entered = threading.Event()
    release_executor = threading.Event()

    def blocking_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        executor_entered.set()
        # Stands in for the detached executor child that never sees SIGTERM.
        release_executor.wait(timeout=30)
        return WorkerExecution(0, "done")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=_api_transport(client)),
        agent.id,
        tmp_path,
        blocking_executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    worker.shutdown_grace_seconds = 0.2

    loop = threading.Thread(target=worker.run_forever, kwargs={"max_iterations": 1})
    loop.start()
    try:
        assert executor_entered.wait(timeout=20), "executor never started"
        assert cp.get_task(task.id).state == TaskState.RUNNING.value
        assert cp.get_task(task.id).lease_id is not None

        # Exactly what the OS-installed handler does on SIGTERM.
        worker._handle_shutdown_signal(signal.SIGTERM)

        # Without the fix this never happens: the flag is only read between
        # iterations, so the lease lives until it expires 900s later.
        assert _wait_for(lambda: cp.get_task(task.id).state == TaskState.OPEN.value), (
            "shutdown did not release the lease within the grace deadline"
        )
        released = cp.get_task(task.id)
        assert released.lease_id is None
        assert released.owner_agent_id is None
        # The offline heartbeat follows the release; the hub needs both to tell
        # a departing worker from one that simply stopped answering.
        assert _wait_for(lambda: cp.get_agent(agent.id).status == "offline"), (
            "the worker never marked itself offline on the way out"
        )
    finally:
        release_executor.set()
        loop.join(timeout=30)


def test_second_signal_abandons_immediately_without_waiting_for_the_grace(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = _register_worker(cp)
    task = cp.create_task("Long task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    executor_entered = threading.Event()
    release_executor = threading.Event()

    def blocking_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        executor_entered.set()
        release_executor.wait(timeout=30)
        return WorkerExecution(0, "done")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=_api_transport(client)),
        agent.id,
        tmp_path,
        blocking_executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    # A grace long enough that only the SECOND signal can explain a release.
    worker.shutdown_grace_seconds = 600.0

    loop = threading.Thread(target=worker.run_forever, kwargs={"max_iterations": 1})
    loop.start()
    try:
        assert executor_entered.wait(timeout=20), "executor never started"
        worker._handle_shutdown_signal(signal.SIGTERM)
        assert cp.get_task(task.id).state == TaskState.RUNNING.value
        worker._handle_shutdown_signal(signal.SIGTERM)
        assert _wait_for(lambda: cp.get_task(task.id).state == TaskState.OPEN.value), (
            "a second SIGTERM did not abandon the assignment"
        )
    finally:
        release_executor.set()
        loop.join(timeout=30)


def test_abandonment_publishes_a_shutdown_observation(tmp_path: Path):
    # "Worker restarted" and "worker crashed" must be distinguishable in the
    # ledger; the only way to tell is an observation the worker publishes on
    # its way out.
    cp = ControlPlane.in_memory()
    agent = _register_worker(cp)
    task = cp.create_task("Long task", required_capabilities=["python"])
    client = TestClient(create_app(control_plane=cp))

    executor_entered = threading.Event()
    release_executor = threading.Event()

    def blocking_executor(task_payload: Dict[str, Any], task_dir: Path) -> WorkerExecution:
        executor_entered.set()
        release_executor.wait(timeout=30)
        return WorkerExecution(0, "done")

    worker = MacWorker(
        MacApiClient("http://mac.test", transport=_api_transport(client)),
        agent.id,
        tmp_path,
        blocking_executor,
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    worker.shutdown_grace_seconds = 0.2

    loop = threading.Thread(target=worker.run_forever, kwargs={"max_iterations": 1})
    loop.start()
    try:
        assert executor_entered.wait(timeout=20)
        worker._handle_shutdown_signal(signal.SIGTERM)
        assert _wait_for(lambda: cp.get_task(task.id).state == TaskState.OPEN.value)
        assert _wait_for(
            lambda: bool(cp.list_observability(name="worker.shutdown.abandoned", limit=5))
        ), "no worker.shutdown.abandoned observation was published"
    finally:
        release_executor.set()
        loop.join(timeout=30)


def test_abandonment_is_a_no_op_when_no_assignment_is_held(tmp_path: Path):
    cp = ControlPlane.in_memory()
    agent = _register_worker(cp)
    client = TestClient(create_app(control_plane=cp))
    worker = MacWorker(
        MacApiClient("http://mac.test", transport=_api_transport(client)),
        agent.id,
        tmp_path,
        lambda _task, _dir: WorkerExecution(0, "unused"),
        attestation_key=cp._agent_attestation_key(agent.id),
    )
    detail = worker._abandon_active_assignment("test")
    assert detail["held_assignment"] is False
    assert detail["heartbeat_offline"] is True
    # Idempotent: a second call does nothing at all.
    assert worker._abandon_active_assignment("test") == {}


def test_default_grace_fits_inside_the_units_stop_timeout(tmp_path: Path):
    # The whole point of the deadline is to release BEFORE systemd's SIGKILL.
    # mac-agent-service ships TimeoutStopSec=600 with KillMode=mixed, so a
    # default at or above that would abandon nothing.
    unit = Path(__file__).resolve().parents[1] / "deploy" / "fleet-node-install.sh"
    text = unit.read_text(encoding="utf-8")
    assert "TimeoutStopSec=600" in text
    assert 0 < DEFAULT_SHUTDOWN_GRACE_SECONDS < 600

    worker = MacWorker(
        object(),  # type: ignore[arg-type]
        "agent_default",
        tmp_path,
        lambda _task, _dir: WorkerExecution(0, "unused"),
    )
    assert worker.shutdown_grace_seconds == DEFAULT_SHUTDOWN_GRACE_SECONDS


def test_delivery_drain_thread_is_joined_before_shutdown_completes(tmp_path: Path):
    # A drain already inside _process_human_delivery_outbox (an HTTP round trip
    # plus a local openclaw-message delivery) must not still be running when
    # _shutdown() posts offline and the process exits: a message delivered
    # locally but never acked is redelivered on the next start.
    worker = MacWorker(
        object(),  # type: ignore[arg-type] - no HTTP in this test
        "agent_drain",
        tmp_path,
        lambda _task, _dir: WorkerExecution(0, "unused"),
    )
    worker.delivery_drain_interval_seconds = 0.01

    entered = threading.Event()
    finished = threading.Event()

    def slow_drain() -> None:
        entered.set()
        time.sleep(0.5)
        finished.set()

    worker._process_human_delivery_outbox = slow_drain  # type: ignore[assignment]
    worker._start_delivery_drain_thread()
    thread = worker._delivery_drain_thread
    assert thread is not None
    assert entered.wait(timeout=5), "drain never ran"

    worker._stop_delivery_drain_thread()

    assert finished.is_set(), (
        "the drain was still in flight when shutdown returned; it was stopped without a join"
    )
    assert not thread.is_alive()


# --------------------------------------------------------------------------
# Process-level proof: a real SIGTERM followed by a real SIGKILL, against a
# fake hub that records what it was told.
# --------------------------------------------------------------------------


class _FakeHub:
    """Records every request; answers the handful of paths the worker needs."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self._lock = threading.Lock()
        self._claimed = False
        hub = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: Any) -> None:  # noqa: A003
                pass

            def _respond(self, payload: Any) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _record(self, method: str) -> Dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else None
                except ValueError:
                    payload = None
                entry = {"method": method, "path": self.path, "payload": payload}
                with hub._lock:
                    hub.requests.append(entry)
                return entry

            def do_GET(self) -> None:  # noqa: N802
                self._record("GET")
                if self.path.startswith("/agents/"):
                    self._respond({"id": "agent_fake", "dispatch_hold": False})
                    return
                self._respond({})

            def do_POST(self) -> None:  # noqa: N802
                entry = self._record("POST")
                path = entry["path"]
                if path.endswith("/claim-next"):
                    with hub._lock:
                        first = not hub._claimed
                        hub._claimed = True
                    if first:
                        self._respond(
                            {
                                "task": {
                                    "id": "task_fake",
                                    "title": "blocking task",
                                    "metadata": {},
                                },
                                "lease": {"id": "lease_fake"},
                            }
                        )
                        return
                    self._respond(None)
                    return
                self._respond({})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return "http://%s:%d" % (host, port)

    def __enter__(self) -> "_FakeHub":
        self._thread.start()
        return self

    def __exit__(self, *_exc: Any) -> None:
        self._server.shutdown()
        self._server.server_close()

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.requests)


_CHILD_SCRIPT = """
import sys, time
from pathlib import Path
from mac.hermes_adapter import MacApiClient
from mac.worker import MacWorker, WorkerExecution

hub_url, workspace = sys.argv[1], sys.argv[2]


def executor(task, task_dir):
    # The detached executor child: it does not see SIGTERM.
    time.sleep(300)
    return WorkerExecution(0, "never")


class _Worker(MacWorker):
    def _reconcile_runtime_deps_best_effort(self):
        return None

    def _prepare_task_workspace(self, task, lease):
        directory = Path(workspace) / "task"
        directory.mkdir(parents=True, exist_ok=True)
        return directory


worker = _Worker(
    MacApiClient(hub_url, token="test-token"),
    "agent_fake",
    Path(workspace),
    executor,
    poll_interval_seconds=0.2,
    lease_renew_interval_seconds=0.5,
)
worker.shutdown_grace_seconds = 1.0
worker.delivery_drain_interval_seconds = 0
worker.run_forever()
"""


def test_real_sigterm_then_sigkill_still_releases_the_lease(tmp_path: Path):
    script = tmp_path / "run_worker.py"
    script.write_text(_CHILD_SCRIPT, encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    env = dict(os.environ)
    env["MAC_WORKER_SHUTDOWN_GRACE_SECONDS"] = "1"
    env["PYTHONUNBUFFERED"] = "1"

    with _FakeHub() as hub:
        child = subprocess.Popen(
            [sys.executable, str(script), hub.url, str(workspace)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:

            def started() -> bool:
                return any("/tasks/task_fake/start" in entry["path"] for entry in hub.snapshot())

            assert _wait_for(started, timeout=60), (
                "worker never began executing the fake task; hub saw %r"
                % [entry["path"] for entry in hub.snapshot()][:40]
            )

            child.send_signal(signal.SIGTERM)
            # Past the grace, then the hard kill the unit's TimeoutStopSec
            # eventually delivers. Whatever the hub learned, it learned before
            # this point.
            time.sleep(4)
            child.send_signal(signal.SIGKILL)
            child.wait(timeout=30)

            observed = hub.snapshot()
            released = [
                entry
                for entry in observed
                if "/tasks/task_fake/transition" in entry["path"]
                and (entry["payload"] or {}).get("target_state") == "open"
            ]
            offline = [
                entry
                for entry in observed
                if "/heartbeat" in entry["path"]
                and (entry["payload"] or {}).get("status") == "offline"
            ]
            assert released or offline, (
                "SIGTERM+SIGKILL left the lease held: the hub was told nothing. "
                "Paths seen: %r" % [entry["path"] for entry in observed][-40:]
            )
            assert released, (
                "the held lease was never released; the task stays undispatchable until it expires"
            )
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
