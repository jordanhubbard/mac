"""Real POSIX preflight reservation/refusal controls; no peer PID operations."""

from contextlib import contextmanager, ExitStack
import errno
import os
import socket
import sys
import threading
import time
import uuid

import pytest

from mac import world4d_service as service
from tests.world4d_nonce_fixture import bind_listener, probe, serve

pytestmark = [
    pytest.mark.process_e2e,
    pytest.mark.skipif(os.name != "posix", reason="POSIX World4D service"),
]


def free_ports(count):
    # Allocate the whole fixture set together to avoid duplicate ephemeral ports.
    with ExitStack() as held:
        listeners = [held.enter_context(socket.socket()) for _ in range(count)]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in listeners]


def worlds(ports):
    return [{"name": f"world-{index}", "port": port} for index, port in enumerate(ports)]


@contextmanager
def independent_listener(port=0):
    # A real application identity must survive refusal; TCP acceptance is never
    # our survival oracle. This fixture owns its thread, socket and bounded life.
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        stopping = threading.Event()
        errors = []
        identity = uuid.uuid4().hex
        port = listener.getsockname()[1]

        def run():
            try:
                serve(listener, identity, time.monotonic() + 18, stop_event=stopping)
            except BaseException as exc:
                errors.append(
                    {
                        "type": type(exc).__name__,
                        "errno": exc.errno if isinstance(exc, OSError) else None,
                    }
                )

        thread = threading.Thread(target=run)
        thread.start()
        try:
            assert probe(port, identity)["nonce_reply"] is True
            yield port, identity, thread
        finally:
            stopping.set()
            thread.join(timeout=1)
            assert not thread.is_alive() and not errors


def test_all_world_ports_are_held_together_then_released_for_restart():
    ports = free_ports(2)
    for _restart in range(2):
        with service.reserve_world_ports(worlds(ports)):
            assert all(bind_listener(port)["released"] is False for port in ports)
        assert all(bind_listener(port)["released"] is True for port in ports)


@pytest.mark.parametrize("foreign_index", [0, 1], ids=["first-port", "partial-reservation"])
def test_refuses_real_foreign_listener_without_disturbing_its_identity(foreign_index):
    with independent_listener() as (foreign_port, identity, thread):
        free = free_ports(1)[0]
        ports = [free]
        ports.insert(foreign_index, foreign_port)
        with pytest.raises(
            RuntimeError, match=f"cannot reserve world port {foreign_port}"
        ) as error:
            with service.reserve_world_ports(worlds(ports)):
                raise AssertionError("occupied set must never admit startup")
        assert isinstance(error.value.__cause__, OSError)
        assert error.value.__cause__.errno == errno.EADDRINUSE
        assert thread.is_alive() and probe(foreign_port, identity)["nonce_reply"] is True
        assert bind_listener(free)["released"] is True
    # The actual independent listener has now closed, so a fresh full set must
    # succeed at exactly those ports despite any TCP-only transport acceptance.
    with service.reserve_world_ports(worlds(ports)):
        assert all(bind_listener(port)["released"] is False for port in ports)
    assert all(bind_listener(port)["released"] is True for port in ports)


def test_reservation_body_failure_releases_the_whole_set():
    ports = free_ports(2)
    with pytest.raises(ValueError, match="fixture-body-failure"):
        with service.reserve_world_ports(worlds(ports)):
            raise ValueError("fixture-body-failure")
    assert all(bind_listener(port)["released"] is True for port in ports)


def test_partial_listen_failure_closes_even_the_already_bound_failed_socket(monkeypatch):
    ports = free_ports(2)
    real_socket = socket.socket
    created = []

    class FailingListen:
        def __init__(self, sock):
            self.sock = sock

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.sock.__exit__(*exc)

        def setsockopt(self, *args):
            return self.sock.setsockopt(*args)

        def bind(self, *args):
            return self.sock.bind(*args)

        def listen(self, *_args):
            raise OSError(errno.EIO, "fixture-listen-failure")

    def create(*args, **kwargs):
        sock = real_socket(*args, **kwargs)
        created.append(sock)
        return FailingListen(sock) if len(created) == 2 else sock

    with monkeypatch.context() as patch:
        patch.setattr(service.socket, "socket", create)
        with pytest.raises(RuntimeError, match=f"cannot reserve world port {ports[1]}") as error:
            with service.reserve_world_ports(worlds(ports)):
                raise AssertionError("listen failure must not admit startup")
        assert error.value.__cause__.errno == errno.EIO
    assert len(created) == 2 and all(sock.fileno() == -1 for sock in created)
    assert all(bind_listener(port)["released"] is True for port in ports)


def test_released_world_endpoint_supports_a_real_new_application_identity():
    with independent_listener() as (port, old_identity, _thread):
        assert probe(port, old_identity)["nonce_reply"] is True
    with service.reserve_world_ports(worlds([port])):
        assert bind_listener(port)["released"] is False
    with independent_listener(port) as (same_port, new_identity, thread):
        assert same_port == port and new_identity != old_identity
        assert thread.is_alive() and probe(port, new_identity)["nonce_reply"] is True
        assert probe(port, old_identity)["nonce_reply"] is False
    with service.reserve_world_ports(worlds([port])):
        assert bind_listener(port)["released"] is False


@pytest.mark.parametrize(
    "foreign_blocked", [True, False], ids=["guard-refused", "thread-start-failed"]
)
def test_run_closes_monitor_and_partial_reservations_before_any_world_start(
    tmp_path, monkeypatch, foreign_blocked
):
    with independent_listener() as (foreign_port, identity, foreign_thread):
        monitor, first, second = free_ports(3)
        ports = [first, foreign_port if foreign_blocked else second]
        config = {
            "root": str(tmp_path),
            "source": str(tmp_path),
            "python": sys.executable,
            "home": str(tmp_path),
            "environment_file": str(tmp_path / "unused.json"),
            "monitor_port": monitor,
            "worlds": worlds(ports),
            "storage_guard": False,
        }
        starts = []

        class FailingThread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                starts.append(True)
                raise RuntimeError("fixture-thread-start-failure")

            def is_alive(self):
                return False

        def no_world_start(_self):
            raise AssertionError("no payload may start on preflight or monitor startup failure")

        with monkeypatch.context() as patch:
            patch.setattr(service.threading, "Thread", FailingThread)
            patch.setattr(service.signal, "signal", lambda *_args: None)
            patch.setattr(service, "load_environment", lambda _path: None)
            patch.setattr(service.World, "start", no_world_start)
            expected = (
                "cannot reserve world port" if foreign_blocked else "fixture-thread-start-failure"
            )
            with pytest.raises(RuntimeError, match=expected):
                service.run(config)
        assert len(starts) == (0 if foreign_blocked else 1)
        assert bind_listener(monitor)["released"] is True
        assert bind_listener(first)["released"] is True
        if not foreign_blocked:
            assert bind_listener(second)["released"] is True
        assert foreign_thread.is_alive() and probe(foreign_port, identity)["nonce_reply"] is True
