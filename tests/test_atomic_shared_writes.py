"""Temp-file safety for the writers that share a directory with other processes.

Two independent defects are covered:

**Splice.** ``os.replace`` is atomic; *choosing the same temporary path is not*.
Several writers in this tree wrote a FIXED temp name (``agent-footprint.json.tmp``,
``openshell-policy.yaml.tmp``, ``finalizer-progress.json.tmp``,
``.<name>.partial``) into a directory that many processes share — ``~/.mac`` is
per-USER, not per-agent, and the AgentFS WebDAV share is written by many agents
through a ThreadingHTTPServer. Concurrent writers truncated and interleaved into
one temp file and then renamed the mixture into place. The tests below run real
concurrent writers against one path and assert the file is always EXACTLY one
writer's bytes.

**Durability.** ``os.replace`` is atomic with respect to other processes but not
with respect to a crash: with delayed allocation the rename can be journalled
ahead of the data blocks, leaving a zero-length file. Every reader in this tree
swallows the resulting parse error and substitutes an empty default, so the
failure is silent. The tests below count ``os.fsync`` calls and assert both the
data descriptor and the parent *directory* descriptor are synced.
"""

from __future__ import annotations

import errno
import json
import os
import socket
import stat
import sys
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from pathlib import Path

import pytest

from mac import atomic_file
from mac.atomic_file import atomic_write_text
from mac.webdav_server import WebDAVHandler, WebDAVServer


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------


class _FsyncSpy:
    """Records every ``os.fsync`` and whether its descriptor was a directory."""

    def __init__(self, monkeypatch):
        self.calls = []
        real = os.fsync

        def spy(fd):
            try:
                is_dir = stat.S_ISDIR(os.fstat(fd).st_mode)
            except OSError:
                is_dir = False
            self.calls.append(is_dir)
            return real(fd)

        monkeypatch.setattr(os, "fsync", spy)

    @property
    def files(self) -> int:
        return sum(1 for is_dir in self.calls if not is_dir)

    @property
    def directories(self) -> int:
        return sum(1 for is_dir in self.calls if is_dir)


def _run_forked_writers(write_fns, *, iterations: int) -> None:
    """Run each callable in its own process, concurrently, ``iterations`` times.

    Separate processes (not threads) because the splice this guards against is
    an interleaving of ``write(2)`` calls against one shared temp inode; the GIL
    would otherwise hide it.
    """

    children = []
    for write_fn in write_fns:
        pid = os.fork()
        if pid == 0:  # child
            code = 0
            try:
                for _ in range(iterations):
                    write_fn()
            except BaseException:  # noqa: BLE001 - reported through the exit code
                code = 1
            finally:
                os._exit(code)
        children.append(pid)
    return children


def _assert_never_spliced(path: Path, expected: set, children) -> None:
    """Poll *path* while the writers run; every observation must be intact."""

    bad = []
    seen = 0
    remaining = list(children)
    deadline = time.monotonic() + 120.0
    while remaining:
        if time.monotonic() > deadline:
            for pid in remaining:
                os.kill(pid, 9)
                os.waitpid(pid, 0)
            pytest.fail("forked writers did not finish within 120s")
        for pid in list(remaining):
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                remaining.remove(pid)
                assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, (
                    "writer process %d failed" % pid
                )
        try:
            observed = path.read_text(encoding="utf-8")
        except (FileNotFoundError, UnicodeDecodeError) as exc:
            bad.append(repr(exc))
            continue
        seen += 1
        if observed not in expected:
            bad.append(
                "spliced write: %d bytes, head=%r tail=%r"
                % (len(observed), observed[:60], observed[-60:])
            )
    assert not bad, "concurrent writers corrupted %s:\n%s" % (path, "\n".join(bad[:5]))
    assert seen > 0, "reader never observed the file"


def _filler(marker: str, size: int = 400_000) -> str:
    """A payload big enough that one write is many write(2) calls."""

    return (marker * 64)[:64] * (size // 64)


# --------------------------------------------------------------------------
# The shared helper itself
# --------------------------------------------------------------------------


def test_atomic_write_text_fsyncs_data_and_directory(tmp_path, monkeypatch):
    spy = _FsyncSpy(monkeypatch)
    target = tmp_path / "nested" / "state.json"

    atomic_write_text(target, "payload\n")

    assert target.read_text(encoding="utf-8") == "payload\n"
    assert spy.files >= 1, "the data descriptor was never fsynced before the rename"
    assert spy.directories >= 1, "the parent directory was never fsynced after the rename"


def test_atomic_writer_picks_a_unique_temp_name_per_call(tmp_path):
    names = []
    for _ in range(8):
        with atomic_file.atomic_writer(tmp_path / "shared.json") as handle:
            names.append(
                sorted(p.name for p in tmp_path.iterdir() if p.name != "shared.json")
            )
            handle.write("{}")
    flat = [n for batch in names for n in batch]
    assert len(set(flat)) == len(flat), "temp names collided: %r" % flat


def test_atomic_writer_leaves_the_target_untouched_on_failure(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("original", encoding="utf-8")
    with pytest.raises(ValueError):
        with atomic_file.atomic_writer(target) as handle:
            handle.write("half")
            raise ValueError("boom")
    assert target.read_text(encoding="utf-8") == "original"
    assert list(tmp_path.iterdir()) == [target], "temp file was left behind"


def test_fsync_directory_survives_filesystems_that_refuse_it(tmp_path, monkeypatch):
    def refuse(fd):
        raise OSError(errno.EINVAL, "fsync not supported")

    monkeypatch.setattr(os, "fsync", refuse)
    atomic_file.fsync_directory(tmp_path)  # must not raise


# --------------------------------------------------------------------------
# 1. ~/.mac/agent-footprint.json  (worker_runtime_deps)
# --------------------------------------------------------------------------


def _footprint_worker(home: Path):
    from mac.worker_runtime_deps import RuntimeDepsMixin

    instance = object.__new__(RuntimeDepsMixin)
    instance._mac_home = lambda: home
    return instance


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_concurrent_agents_never_splice_the_shared_footprint(tmp_path):
    """``~/.mac`` is per-USER: every worker, CLI run and agent startup writes here.

    With one fixed ``agent-footprint.json.tmp`` the writers truncate and
    interleave into each other's temp file, and ``_load_footprint`` swallows the
    resulting JSONDecodeError and returns ``{}`` — silently erasing the record of
    what is installed in the shared venv.
    """

    home = tmp_path / "machome"
    home.mkdir()
    instance = _footprint_worker(home)
    path = home / "agent-footprint.json"

    payloads = [
        {"pip": [{"name": "pkg-%s" % marker}], "filler": _filler(marker)}
        for marker in ("a", "b", "c")
    ]
    expected = set()
    for payload in payloads:
        instance._write_footprint(payload)
        expected.add(path.read_text(encoding="utf-8"))

    children = _run_forked_writers(
        [lambda p=p: instance._write_footprint(p) for p in payloads], iterations=25
    )
    _assert_never_spliced(path, expected, children)
    assert json.loads(path.read_text(encoding="utf-8"))["pip"]


def test_footprint_write_is_durable(tmp_path, monkeypatch):
    home = tmp_path / "machome"
    home.mkdir()
    spy = _FsyncSpy(monkeypatch)
    _footprint_worker(home)._write_footprint({"pip": []})
    assert spy.files >= 1 and spy.directories >= 1


# --------------------------------------------------------------------------
# 1b. the shared-venv install lock must not fail open
# --------------------------------------------------------------------------


def _lockable_worker(home: Path):
    from mac.worker_runtime_deps import RuntimeDepsMixin

    instance = object.__new__(RuntimeDepsMixin)
    instance._mac_home = lambda: home
    instance._agent_venv_python = lambda: sys.executable
    instance._observe_log = lambda *a, **k: None
    return instance


def test_install_lock_failure_raises_instead_of_running_pip_unserialized(
    tmp_path, monkeypatch
):
    """A swallowed flock error meant installing into the shared venv unlocked.

    ``except Exception: pass`` around ``fcntl.flock`` returned a handle that the
    caller treated as an exclusive lock, so two agents ran ``pip install`` into
    ``~/.mac/venv`` at the same time while each believed it was serialized.
    """

    import fcntl

    home = tmp_path / "machome"
    home.mkdir()
    instance = _lockable_worker(home)

    def refuse(handle, operation):
        raise OSError(errno.ENOLCK, "no locks available")

    monkeypatch.setattr(fcntl, "flock", refuse)
    with pytest.raises(RuntimeError, match="install lock"):
        instance._install_lock()


def test_footprint_update_on_the_already_satisfied_fast_path_holds_the_lock(tmp_path):
    """The fast path read-modify-writes the shared footprint too.

    ``_update_footprint`` loads, mutates and rewrites a host-shared file, so an
    unlocked call on the "already satisfied" path loses a concurrent agent's
    update outright. Every call must sit inside the install lock.
    """

    home = tmp_path / "machome"
    home.mkdir()
    instance = _lockable_worker(home)
    instance._pip_installed = lambda py: {"nemo-relay": "0.3.0"}
    instance._report_footprint = lambda fp: events.append("report")

    events = []
    real_lock = instance._install_lock

    class _Tracked:
        def __init__(self, handle):
            self._handle = handle

        def close(self):
            events.append("unlock")
            self._handle.close()

    def tracked_lock():
        events.append("lock")
        return _Tracked(real_lock())

    instance._install_lock = tracked_lock
    real_write = instance._write_footprint
    instance._write_footprint = lambda fp: (events.append("write"), real_write(fp))[1]

    result = instance.ensure_pip(["nemo-relay==0.3.0"])

    assert result.get("skipped") == "already satisfied"
    assert events.index("lock") < events.index("write") < events.index("unlock")
    # The hub report is network I/O; holding a host-wide lock across it would
    # let an unreachable hub block every other agent's installs.
    assert events.index("unlock") < events.index("report")


# --------------------------------------------------------------------------
# 2. ~/.mac/openshell-policy.yaml  (worker)
# --------------------------------------------------------------------------


POLICY_TEMPLATE = """version: 1

network_policies:
  mac_hub:
    name: mac-hub-%s
    endpoints:
      - host: hub.example.com
        port: 8789
# %s
"""


class _PolicyClient:
    def __init__(self, assigned):
        self._assigned = assigned

    def get(self, path):
        return dict(self._assigned)

    def post(self, path, payload):
        return {}


def _policy_worker(tmp_path, text):
    from mac import worker
    from mac.openshell_service import policy_checksum

    client = _PolicyClient(
        {
            "schema": "mac.openshell.assigned_policy.v1",
            "agent_id": "agent-1",
            "policy_id": "ospol_1",
            "policy_name": "fleet",
            "version": 3,
            "checksum": policy_checksum(text),
            "policy_text": text,
        }
    )
    return worker.MacWorker(
        client,
        "agent-1",
        tmp_path,
        lambda _task, _path: worker.WorkerExecution(0, "ok"),
        self_update_repo=tmp_path,
    )


@pytest.fixture
def policy_home(tmp_path, monkeypatch):
    from mac import worker

    home = tmp_path / "machome"
    home.mkdir()
    monkeypatch.setattr(worker.mac_paths, "mac_home", lambda: home)
    monkeypatch.delenv("MAC_OPENSHELL_POLICY", raising=False)
    return home


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_concurrent_policy_syncs_never_install_a_spliced_policy(policy_home, tmp_path):
    """A spliced policy is still valid YAML, so egress restriction vanishes quietly.

    The comment above this write already stated the threat model: a truncated
    policy "being a valid YAML prefix, could silently drop the network_policies
    mapping entirely". A fixed ``openshell-policy.yaml.tmp`` in the per-user
    ``~/.mac`` gave every worker on the host the same temp file to write it in.
    """

    target = policy_home / "openshell-policy.yaml"
    texts = [
        POLICY_TEMPLATE % (marker, _filler(marker, 1_200_000))
        for marker in ("a", "b", "c")
    ]
    workers = [_policy_worker(tmp_path / ("w" + m), t) for m, t in zip("abc", texts)]

    expected = set()
    for instance in workers:
        instance._last_openshell_policy_sync = None
        instance._maybe_sync_openshell_policy()
        expected.add(target.read_text(encoding="utf-8"))
    assert len(expected) == 3

    def writer(instance):
        def run():
            instance._last_openshell_policy_sync = None
            instance._maybe_sync_openshell_policy()

        return run

    children = _run_forked_writers([writer(w) for w in workers], iterations=60)
    _assert_never_spliced(target, expected, children)

    import yaml

    parsed = yaml.safe_load(target.read_text(encoding="utf-8"))
    assert parsed["network_policies"]["mac_hub"]["endpoints"]


def test_policy_install_is_durable(policy_home, tmp_path, monkeypatch):
    text = POLICY_TEMPLATE % ("a", "a")
    instance = _policy_worker(tmp_path / "w", text)
    spy = _FsyncSpy(monkeypatch)
    instance._maybe_sync_openshell_policy()
    target = policy_home / "openshell-policy.yaml"
    assert target.read_text(encoding="utf-8") == text
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert spy.files >= 1, "policy data was never fsynced before the rename"
    assert spy.directories >= 1, "the policy directory was never fsynced"


# --------------------------------------------------------------------------
# 3. AgentFS WebDAV share  (webdav_server)
# --------------------------------------------------------------------------


class _DavFixture:
    def __init__(self, tmp_path, token="tok"):
        self.root = tmp_path / "agentfs"
        self.root.mkdir()
        self.token = token
        self.server = WebDAVServer(
            ("127.0.0.1", 0),
            WebDAVHandler,
            root=self.root,
            public_prefix="/agentfs/",
            max_upload_bytes=8 * 1024 * 1024,
            write_token=token,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def dav(tmp_path):
    fixture = _DavFixture(tmp_path)
    try:
        yield fixture
    finally:
        fixture.close()


def _trickle_put(port, token, url_path, body, *, chunks=8, delay=0.02, start_delay=0.0):
    """PUT *body* slowly on a raw socket so concurrent uploads really overlap."""

    time.sleep(start_delay)
    conn = socket.create_connection(("127.0.0.1", port), timeout=10)
    try:
        header = (
            "PUT %s HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer %s\r\n"
            "Content-Length: %d\r\nConnection: close\r\n\r\n"
            % (url_path, token, len(body))
        )
        conn.sendall(header.encode())
        step = max(1, len(body) // chunks)
        for offset in range(0, len(body), step):
            conn.sendall(body[offset : offset + step])
            time.sleep(delay)
        conn.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            piece = conn.recv(4096)
            if not piece:
                break
            response += piece
        return response
    finally:
        conn.close()


def test_concurrent_puts_to_one_path_never_splice(dav):
    """Two agents PUTting one AgentFS path must not interleave into one temp file.

    With a fixed ``.<name>.partial`` the two uploads shared a temp inode: the
    first rename installed a mixture of both bodies and answered 201 with a byte
    count it had counted itself rather than one it had written, and the second
    rename raised FileNotFoundError -> 500.
    """

    body_a = b"A" * 200_000
    body_b = b"B" * 200_000
    results = {}

    def run(name, body, start_delay):
        results[name] = _trickle_put(
            dav.port, dav.token, "/agentfs/race.bin", body, start_delay=start_delay
        )

    threads = [
        threading.Thread(target=run, args=("a", body_a, 0.0)),
        threading.Thread(target=run, args=("b", body_b, 0.03)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    for name, response in results.items():
        assert b"201" in response.split(b"\r\n", 1)[0], (name, response[:120])

    stored = (dav.root / "race.bin").read_bytes()
    assert stored in (body_a, body_b), (
        "stored file is a splice: %d bytes, %d 'A' + %d 'B'"
        % (len(stored), stored.count(b"A"), stored.count(b"B"))
    )


def test_truncated_upload_is_still_rejected_and_leaves_no_temp(dav):
    conn = socket.create_connection(("127.0.0.1", dav.port), timeout=10)
    try:
        conn.sendall(
            (
                "PUT /agentfs/short.bin HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                "Authorization: Bearer %s\r\nContent-Length: 100\r\n"
                "Connection: close\r\n\r\n" % dav.token
            ).encode()
        )
        conn.sendall(b"x" * 10)
        conn.shutdown(socket.SHUT_WR)
        response = b""
        while True:
            piece = conn.recv(4096)
            if not piece:
                break
            response += piece
    finally:
        conn.close()
    assert b"400" in response.split(b"\r\n", 1)[0]
    assert not (dav.root / "short.bin").exists()
    assert list(dav.root.iterdir()) == [], "a temp file survived the failed upload"


def test_delete_of_a_vanished_file_is_404_not_500(dav, monkeypatch):
    """``if target.is_file(): target.unlink()`` is a TOCTOU on a shared share.

    Another agent's DELETE (or a PUT's rename) can remove the file between the
    check and the unlink, and the resulting FileNotFoundError escaped the
    handler as a 500 instead of the 404 the client should see.
    """

    (dav.root / "gone.txt").write_text("bye\n", encoding="utf-8")

    real_unlink = Path.unlink

    def vanishing(self, *args, **kwargs):
        if self.name == "gone.txt":
            real_unlink(self, *args, **kwargs)
            raise FileNotFoundError(2, "No such file or directory", str(self))
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", vanishing)

    request = urllib.request.Request(
        dav.base + "/agentfs/gone.txt",
        method="DELETE",
        headers={"Authorization": "Bearer %s" % dav.token},
    )
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request, timeout=10)  # noqa: S310
    assert excinfo.value.code == HTTPStatus.NOT_FOUND


# --------------------------------------------------------------------------
# 4. finalizer-progress.json  (executor_finalizer)
# --------------------------------------------------------------------------


def _progress_context(workspace: Path, phase: str):
    from mac.executor_finalizer import _FinalizerPhaseContext

    return _FinalizerPhaseContext(workspace, "task_1", phase)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_concurrent_finalizer_phases_never_splice_progress(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    path = workspace / "finalizer-progress.json"
    contexts = [_progress_context(workspace, "phase-%s" % m) for m in ("a", "b", "c")]
    fillers = {ctx.phase: _filler(ctx.phase[-1]) for ctx in contexts}

    expected = set()
    for ctx in contexts:
        ctx._write_progress("running", filler=fillers[ctx.phase])
        expected.add(path.read_text(encoding="utf-8"))
    assert len(expected) == 3

    children = _run_forked_writers(
        [
            lambda c=c: c._write_progress("running", filler=fillers[c.phase])
            for c in contexts
        ],
        iterations=25,
    )
    _assert_never_spliced(path, expected, children)
    assert json.loads(path.read_text(encoding="utf-8"))["schema"]


def test_finalizer_progress_write_is_durable(tmp_path, monkeypatch):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    ctx = _progress_context(workspace, "phase-a")
    spy = _FsyncSpy(monkeypatch)
    ctx._write_progress("running")
    assert spy.files >= 1 and spy.directories >= 1


# --------------------------------------------------------------------------
# 5. the "atomic" writers that renamed without ever syncing
# --------------------------------------------------------------------------


def test_human_interface_profile_write_is_durable(tmp_path, monkeypatch):
    from mac import human_interface_profile

    spy = _FsyncSpy(monkeypatch)
    target = tmp_path / "profile" / "SOUL.md"
    human_interface_profile._atomic_write(target, "soul\n")
    assert target.read_text(encoding="utf-8") == "soul\n"
    assert spy.files >= 1 and spy.directories >= 1


def test_hermes_config_surface_writes_are_durable(tmp_path, monkeypatch):
    from mac import hermes_config_surface

    spy = _FsyncSpy(monkeypatch)
    hermes_config_surface._atomic_yaml_write(tmp_path / "cfg" / "config.yaml", {"a": 1})
    assert spy.files >= 1 and spy.directories >= 1

    spy2 = _FsyncSpy(monkeypatch)
    hermes_config_surface._atomic_text_write(tmp_path / "cfg" / ".env", "A=1\n")
    assert spy2.files >= 1 and spy2.directories >= 1


def test_executor_directive_queue_write_is_durable(tmp_path, monkeypatch):
    from mac.executor_directive import ExecutorDirectiveQueue, ExecutorDirectiveRecord

    queue = ExecutorDirectiveQueue(tmp_path / "queue" / "directives.json")
    spy = _FsyncSpy(monkeypatch)
    assert queue.enqueue(
        ExecutorDirectiveRecord(
            stream_id="s1",
            task_id="task_1",
            correlation_id="c1",
            message="hello",
            issued_by="operator",
            enqueued_at="2026-08-16T00:00:00+00:00",
        )
    )
    assert spy.files >= 1 and spy.directories >= 1
    assert [r.stream_id for r in queue.all_records()] == ["s1"]


def test_worker_atomic_write_text_is_durable_and_uniquely_named(tmp_path, monkeypatch):
    from mac import worker

    spy = _FsyncSpy(monkeypatch)
    target = tmp_path / "marker.txt"
    worker._atomic_write_text(target, "marker\n")
    assert target.read_text(encoding="utf-8") == "marker\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert spy.files >= 1 and spy.directories >= 1
    # A pid-suffixed temp name is shared by every thread in the process.
    assert not list(tmp_path.glob("marker.txt.tmp.*"))
