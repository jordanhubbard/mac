"""Actual Linux guardian controls; requires the SAME approved sandbox as originals.

New platform guards do not skip or change either original lifecycle test. Every
sacrificial payload has an autonomous18s deadline; test failures cannot require
numeric-PID guessing for cleanup. No custom-clone capability claim is made.
"""

import errno
import hashlib
import json
import math
import os
from pathlib import Path
import signal
import re
import threading
import uuid
import selectors
import socket
import subprocess
import sys
import time

import pytest

from mac import world4d_owned_child as owned
from tests.world4d_nonce_fixture import probe, bind_listener, serve

PROTOCOL_FILE = Path(__file__).with_name("world4d_nonce_fixture.py").resolve()

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux subreaper integration"),
    pytest.mark.process_e2e,
]


def eventually(condition, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = condition()
            if result:
                return result
        except (OSError, ValueError):
            pass
        time.sleep(0.02)
    raise AssertionError("owned fixture did not reach required state")


class FixtureEvidence:
    def __init__(self, root):
        self.root = root
        self.counter = 0

    def write(self, name, value):
        target = self.root / (name + ".json")
        target.write_text(json.dumps(value, sort_keys=True) + "\n")
        target.chmod(0o600)

    def helper(self):
        raw = Path(owned._HELPER).read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        target = self.root / ("executed-helper-" + digest + ".py")
        target.write_bytes(raw)
        target.chmod(0o400)
        self.write("helper-identity", {"sha256": digest, "source": target.name})

    def endpoint(self, label, port, identity):
        result = probe(port, identity)
        self.counter += 1
        self.write(
            f"endpoint-{self.counter:03d}-{label}", {"port": port, "identity": identity, **result}
        )
        return result

    def stopped(self, child, port, identity):
        after = self.endpoint("owned-after", port, identity)
        released = bind_listener(port)
        self.write("bind-listen-release", released)
        events = self.guardian(child)
        # Gather every observation before assertions, so an earlier transport
        # assertion cannot erase ECHILD/group/foreign-survival evidence.
        return after, released, events

    def guardian(self, child):
        raw = child.telemetry_path.read_bytes()
        events = [json.loads(line) for line in raw.splitlines()]
        self.write(
            "guardian-proof",
            {
                "drained": child._drained,
                "payload_returncode": child._exit,
                "telemetry_file": child.telemetry_path.name,
                "telemetry_sha256": hashlib.sha256(raw).hexdigest(),
                "events": events,
            },
        )
        return events


@pytest.fixture(autouse=True)
def evidence(tmp_path, request):
    # Full runner clears MAC_*; derive ONLY this task's ancestor, never scan a
    # peer tree or read task credentials. Keep exact sources/telemetry outside
    # disposable pytest temp directories even on a failed full-run case.
    task_root = None
    current = Path.cwd().resolve()
    for parent in (current, *list(current.parents)[:5]):
        if re.fullmatch(r"task_[0-9a-f]{32}", parent.name) and (parent / "task.json").is_file():
            task_root = parent
            break
    name = re.sub(r"[^A-Za-z0-9_-]", "_", request.node.name)[:100]
    root = (task_root / "guardian-fixture-evidence" if task_root else tmp_path) / (
        name + "-" + uuid.uuid4().hex
    )
    root.mkdir(parents=True, mode=0o700)
    value = FixtureEvidence(root)
    value.write("fixture", {"schema": "mac.world4d.fixture_evidence.v1", "case": request.node.name})
    # Always retain the reviewed base source as well as the executed injected
    # helper, without importing/copying argv/env/provider diagnostics.
    (root / "base-helper.py").write_bytes(Path(owned.__file__).read_bytes())
    (root / "base-helper.py").chmod(0o400)
    (root / "nonce-protocol-source.py").write_bytes(PROTOCOL_FILE.read_bytes())
    (root / "nonce-protocol-source.py").chmod(0o400)
    return value


def zero_custody(event):
    return (
        type(event["children"]) is int
        and event["children"] == 0
        and type(event["terminal"]) is int
        and event["terminal"] == 0
        and event["payload_status_present"] is True
    )


def assert_drain(child, events, expected_exit=None):
    assert child._drained is True and type(child._exit) is int
    assert child._exit == (-signal.SIGKILL if expected_exit is None else expected_exit)
    # An autonomous deadline must not stand in for the required KILL fallback.
    assert any(event["operation"] == "child" and event["phase"] == "kill" for event in events)
    keys = {
        "phase",
        "operation",
        "errno",
        "children",
        "terminal",
        "deadline",
        "payload_status_present",
    }
    assert events and all(set(event) == keys for event in events)

    assert events[-1]["operation"] == "drained" and events[-1]["phase"] == "drained"
    assert zero_custody(events[-1])
    assert any(
        event["operation"] == "echild" and event["phase"] == "reap" and zero_custody(event)
        for event in events[:-1]
    )


def assert_released(after, released):
    assert after["nonce_reply"] is False
    assert released["released"] is True, released


def port_number():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
def denied_group_helper(evidence, monkeypatch):
    # Instrument the ACTUALLY EXECUTED helper copy, not the test parent's os.
    # No product permission knob or runtime/policy change exists for this.
    source = Path(owned.__file__).read_text()
    marker = 'if __name__ == "__main__":'
    assert source.count(marker) == 1
    injection = """
def _fixture_group_denial(self, payload, decide):
    decide()
    raise PermissionError(errno.EPERM, "forced own-group test")
_Kernel.group_signal = _fixture_group_denial

"""
    helper = evidence.root / "forced-group-helper.py"
    helper.write_text(source.replace(marker, injection + marker))
    monkeypatch.setattr(owned, "_HELPER", helper)
    return helper


TREE = """
import json,os,signal,socket,sys,time,runpy
from pathlib import Path
port=int(sys.argv[1]); ready=Path(sys.argv[2]); leader_exit=sys.argv[3]=='exit'
identity=sys.argv[4];serve=runpy.run_path(sys.argv[5])['serve']
end=time.monotonic()+18
signal.signal(signal.SIGTERM, signal.SIG_IGN)
first=os.fork()
if first==0:
    os.setsid()
    second=os.fork()
    if second:
        os._exit(0)
    with socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
        sock.bind(('127.0.0.1',port));sock.listen()
        ready.write_text(json.dumps({'ready':True,'identity':identity,'port':port}))
        serve(sock,identity,end)
    os._exit(0)
while not ready.exists() and time.monotonic()<end:
    time.sleep(.01)
if leader_exit:
    os._exit(0)
while time.monotonic()<end:
    time.sleep(.05)
"""


def start_tree(evidence, *, leader_exit=False):
    port = port_number()
    identity = uuid.uuid4().hex
    ready = evidence.root / "listener-ready.json"
    evidence.helper()
    child = owned.OwnedChild.start(
        [
            sys.executable,
            "-c",
            TREE,
            str(port),
            str(ready),
            "exit" if leader_exit else "stay",
            identity,
            str(PROTOCOL_FILE),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        telemetry_dir=evidence.root,
    )
    try:
        eventually(
            lambda: (
                ready.exists() and evidence.endpoint("owned-before", port, identity)["nonce_reply"]
            )
        )
        return child, port, identity
    except BaseException:
        finish(child)
        raise


def finish(child):
    # Closing the owned control writer forces cleanup. Never guess numeric
    # targets, including if an earlier assertion encountered a custody failure.
    failing = sys.exc_info()[0] is not None
    try:
        child.stop(force=True)
    except owned.CustodyError:
        if not failing:
            raise
    finally:
        for stream in (child._process.stdin, child._process.stdout, child._process.stderr):
            if stream is not None:
                stream.close()


def test_real_forced_eperm_nested_term_ignoring_listener_drains(
    tmp_path, denied_group_helper, evidence
):
    child, port, identity = start_tree(evidence)
    try:
        child.stop()
        after, released, events = evidence.stopped(child, port, identity)
        # Verify the shared original deadline and ordered lifecycle receipts.
        # Existing virtual-clock controls prove the exact boundary sends; the
        # real stop keeps its product bound and the payload's autonomous18s exit.
        assert (owned.TERM_SECONDS, owned.KILL_SECONDS) == (5.0, 5.0)
        first_stop = child._first_stop
        assert type(first_stop) in (int, float) and math.isfinite(first_stop)
        original_deadline = first_stop + owned.TERM_SECONDS + owned.KILL_SECONDS
        stop_index = next(i for i, event in enumerate(events) if event["operation"] == "stop")
        assert events[stop_index]["phase"] == "term"
        assert all(event["deadline"] is None for event in events[:stop_index])
        assert all(event["deadline"] == original_deadline for event in events[stop_index:])
        send_phases = [event["phase"] for event in events if event["operation"] == "child"]
        assert set(send_phases) == {"term", "kill"}
        kill_index = send_phases.index("kill")
        assert all(phase == "term" for phase in send_phases[:kill_index])
        assert all(phase == "kill" for phase in send_phases[kill_index:])
        reap_index = next(i for i, event in enumerate(events) if event["phase"] == "reap")
        final = events[reap_index:]
        assert [(event["operation"], event["phase"]) for event in final] == [
            ("echild", "reap"),
            ("reap", "reap"),
            ("drained", "drained"),
        ]
        assert all(zero_custody(event) and event["errno"] == 0 for event in final)
        assert_drain(child, events)
        assert any(e["operation"] == "group" and e["errno"] == errno.EPERM for e in events)
        assert any(e["operation"] == "child" for e in events)
        assert_released(after, released)
    finally:
        finish(child)


def test_exited_payload_still_drains_detached_adopted_listener(
    tmp_path, denied_group_helper, evidence
):
    child, port, identity = start_tree(evidence, leader_exit=True)
    try:
        # The exact owned nonce was live before waiting for the keeper, so a
        # generic proxy accept cannot stand in for initial application readiness.
        code = child.wait(timeout=12)
        child.stop()
        after, released, events = evidence.stopped(child, port, identity)
        assert code == 0
        assert_drain(child, events, expected_exit=0)
        assert_released(after, released)
    finally:
        finish(child)


def test_explicit_force_poll_and_wait_keep_one_first_stop(tmp_path, denied_group_helper, evidence):
    child, port, identity = start_tree(evidence)
    try:
        assert child.poll() is None
        with pytest.raises(subprocess.TimeoutExpired):
            child.wait(timeout=0.01)
        child.request_stop()
        first = child._first_stop
        child.request_stop(force=True)
        child.stop(force=True)
        after, released, events = evidence.stopped(child, port, identity)
        assert child._first_stop == first
        assert_drain(child, events)
        assert_released(after, released)
    finally:
        finish(child)


def test_stdio_env_cwd_and_payload_exit_are_preserved(tmp_path, evidence):
    code = "import os,sys; print(os.path.basename(os.getcwd())+':'+sys.stdin.readline().strip());sys.exit(7)"
    evidence.helper()
    result = owned.OwnedChild.run(
        [sys.executable, "-c", code],
        telemetry_dir=evidence.root,
        input="payload-data\n",
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 7
    assert result.stdout == tmp_path.name + ":payload-data\n"
    assert result.stderr == ""


def test_communicate_timeout_cleans_before_raising(tmp_path, denied_group_helper, evidence):
    child, port, identity = start_tree(evidence)
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            child.communicate(timeout=0.05)
        after, released, events = evidence.stopped(child, port, identity)
        assert_drain(child, events)
        assert_released(after, released)
    finally:
        finish(child)


def test_foreign_sibling_listener_survives_owned_cleanup(tmp_path, denied_group_helper, evidence):
    foreign_port = port_number()
    foreign_identity = uuid.uuid4().hex
    foreign = subprocess.Popen(
        [
            sys.executable,
            "-c",
            """
import runpy,socket,sys,time
serve=runpy.run_path(sys.argv[1])['serve']
with socket.socket() as sock:
 sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 sock.bind(('127.0.0.1',int(sys.argv[2])));sock.listen()
 serve(sock,sys.argv[3],time.monotonic()+18,stop_reader=sys.stdin.buffer)
""",
            str(PROTOCOL_FILE),
            str(foreign_port),
            foreign_identity,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    child = None
    try:
        eventually(
            lambda: evidence.endpoint("foreign-before", foreign_port, foreign_identity)[
                "nonce_reply"
            ]
        )
        child, port, identity = start_tree(evidence)
        child.stop(force=True)
        after, released, events = evidence.stopped(child, port, identity)
        foreign_after = evidence.endpoint("foreign-after", foreign_port, foreign_identity)
        wrong_identity = evidence.endpoint("foreign-wrong-identity", foreign_port, identity)
        alive = foreign.poll() is None
        evidence.write(
            "foreign-survival", {"alive": alive, "correct": foreign_after, "wrong": wrong_identity}
        )
        assert_drain(child, events)
        assert alive and foreign_after["nonce_reply"] is True
        assert wrong_identity["nonce_reply"] is False
        assert_released(after, released)
    finally:
        try:
            if child is not None:
                finish(child)
        finally:
            foreign.stdin.close()
            foreign.wait(timeout=2)
            foreign.stdout.close()


def test_supervisor_sigkill_eof_drains_without_payload_watchdog(
    tmp_path, denied_group_helper, evidence
):
    port = port_number()
    identity = uuid.uuid4().hex
    ready = evidence.root / "crash-listener.json"
    evidence.helper()
    driver = """
import importlib.util,sys,time
spec=importlib.util.spec_from_file_location('owned_private',sys.argv[1])
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
m._HELPER=__import__('pathlib').Path(sys.argv[2])
reader,writer=m.os.pipe()
child=m.OwnedChild.start([sys.executable,'-c',sys.argv[3],sys.argv[4],sys.argv[5],'stay',sys.argv[7],sys.argv[8]],parent_reader=reader,telemetry_dir=sys.argv[6])
m.os.close(reader)
time.sleep(18)
"""
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            driver,
            owned.__file__,
            str(denied_group_helper),
            TREE,
            str(port),
            str(ready),
            str(evidence.root),
            identity,
            str(PROTOCOL_FILE),
        ]
    )
    try:
        eventually(
            lambda: (
                ready.exists() and evidence.endpoint("crash-before", port, identity)["nonce_reply"]
            )
        )
        proc.kill()  # exact own supervisor, as in the ORIGINAL crash test
        proc.wait(timeout=5)

        # One original 10s observation budget covers nonce absence AND final
        # ECHILD evidence; no extra wait is added after a failed transport poll.
        def final_events():
            after = evidence.endpoint("crash-after", port, identity)
            events = [
                json.loads(line)
                for path in evidence.root.glob("guardian-*.jsonl")
                for line in path.read_text().splitlines()
            ]
            return (
                events
                if not after["nonce_reply"] and any(e["operation"] == "echild" for e in events)
                else None
            )

        events = eventually(final_events, timeout=10)
        released = bind_listener(port)
        evidence.write("crash-final", {"events": events, "release": released})
        assert any(e["operation"] == "echild" and zero_custody(e) for e in events)
        assert any(e["operation"] in {"failure", "eof"} for e in events)
        assert released["released"] is True
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


def test_real_group_esrch_still_accounts_for_children(tmp_path, monkeypatch, evidence):
    source = Path(owned.__file__).read_text()
    injection = '\ndef _missing_group(self,payload,decide):\n    decide()\n    raise ProcessLookupError(errno.ESRCH,"forced own group")\n_Kernel.group_signal=_missing_group\n\n'
    helper = evidence.root / "missing-group-helper.py"
    helper.write_text(
        source.replace('if __name__ == "__main__":', injection + 'if __name__ == "__main__":')
    )
    monkeypatch.setattr(owned, "_HELPER", helper)
    child, port, identity = start_tree(evidence)
    try:
        child.stop(force=True)
        after, released, events = evidence.stopped(child, port, identity)
        assert_drain(child, events)
        assert any(e["operation"] == "group" and e["errno"] == errno.ESRCH for e in events)
        assert any(e["operation"] == "child" for e in events)
        assert_released(after, released)
    finally:
        finish(child)


def test_missing_executable_has_no_false_ready(tmp_path, monkeypatch, evidence):
    processes = []
    original = owned.subprocess.Popen

    def capture(*args, **kwargs):
        process = original(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(owned.subprocess, "Popen", capture)
    evidence.helper()
    with pytest.raises(owned.CustodyError):
        owned.OwnedChild.start([str(tmp_path / "missing-payload")], telemetry_dir=evidence.root)
    for process in processes:
        assert process.wait(timeout=12) != 0


def test_large_private_configuration_is_not_limited_to_pipe_capacity(evidence):
    value = "x" * 100_000
    evidence.helper()
    result = owned.OwnedChild.run(
        [sys.executable, "-c", "import sys;print(len(sys.argv[1]))", value],
        telemetry_dir=evidence.root,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == 0 and result.stdout == "100000\n"


def test_payload_has_no_keeper_control_or_telemetry_descriptors(tmp_path, evidence):
    code = """
import json,os,stat
leaks=[]
for name in os.listdir('/proc/self/fd'):
 fd=int(name)
 if fd<3:continue
 try:
  info=os.fstat(fd)
 except OSError:continue
 if stat.S_ISFIFO(info.st_mode):leaks.append(fd)
print(json.dumps(leaks))
"""
    evidence.helper()
    result = owned.OwnedChild.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        telemetry_dir=evidence.root,
        timeout=3,
    )
    assert result.returncode == 0 and json.loads(result.stdout) == []


def test_late_term_handler_generation_shares_original_stop_deadline(
    tmp_path, denied_group_helper, evidence
):
    port = port_number()
    identity = uuid.uuid4().hex
    ready = evidence.root / "late-listener.json"
    code = """
import os,signal,socket,sys,time,runpy
from pathlib import Path
end=time.monotonic()+18;made=False
port=int(sys.argv[1]);ready=Path(sys.argv[2]);identity=sys.argv[3]
serve=runpy.run_path(sys.argv[4])['serve']
def term(_signal,_frame):
 global made
 if made:return
 made=True
 time.sleep(.2)
 pid=os.fork()
 if pid==0:
  signal.signal(signal.SIGTERM,signal.SIG_IGN)
  with socket.socket() as sock:
   sock.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
   sock.bind(('127.0.0.1',port));sock.listen()
   ready.write_text('ready')
   serve(sock,identity,end)
  os._exit(0)
 os._exit(0)
signal.signal(signal.SIGTERM,term)
print('handler-ready',flush=True)
while time.monotonic()<end:time.sleep(.05)
"""
    evidence.helper()
    child = owned.OwnedChild.start(
        [sys.executable, "-c", code, str(port), str(ready), identity, str(PROTOCOL_FILE)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        telemetry_dir=evidence.root,
    )
    try:
        with selectors.DefaultSelector() as readable:
            readable.register(child._process.stdout, selectors.EVENT_READ)
            assert readable.select(3), "owned handler did not become ready"
            assert child._process.stdout.readline() == b"handler-ready\n"
        child.request_stop()
        first = child._first_stop
        eventually(
            lambda: (
                ready.exists() and evidence.endpoint("late-before", port, identity)["nonce_reply"]
            )
        )
        child.stop()
        after, released, events = evidence.stopped(child, port, identity)
        assert child._first_stop == first
        assert_drain(child, events, expected_exit=0)
        assert all(
            e["deadline"] == first + owned.TERM_SECONDS + owned.KILL_SECONDS
            for e in events
            if e["deadline"] is not None
        )
        assert any(e["children"] >= 2 for e in events)
        assert_released(after, released)
    finally:
        finish(child)


def test_nonpython_payload_restores_normal_popen_sigpipe_semantics(evidence):
    evidence.helper()
    result = owned.OwnedChild.run(
        ["/bin/sh", "-c", "kill -PIPE $$; printf unexpected"],
        telemetry_dir=evidence.root,
        capture_output=True,
        text=True,
        timeout=3,
    )
    assert result.returncode == -signal.SIGPIPE
    assert result.stdout == ""


def test_loopback_transport_requires_application_identity_not_tcp_accept(evidence):
    # Independent no-server/known-server/same-port replacement control. A
    # bound-but-NOT-listening socket reserves the endpoint without a server;
    # neither a proxy nor another fixture can be mistaken for our application.
    from contextlib import contextmanager

    @contextmanager
    def running(listener, identity):
        stopping = threading.Event()
        errors = []

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
            yield
        finally:
            stopping.set()
            thread.join(timeout=1)
            evidence.write(
                "control-server-errors", {"errors": errors, "thread_stopped": not thread.is_alive()}
            )
            assert not thread.is_alive() and not errors

    first_identity = uuid.uuid4().hex
    second_identity = uuid.uuid4().hex
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        no_server = evidence.endpoint("bound-not-listening", port, first_identity)
        listener.listen(1)
        with running(listener, first_identity):
            known = evidence.endpoint("known-first-server", port, first_identity)
            wrong = evidence.endpoint("known-wrong-identity", port, second_identity)
    # Actual bind/listen release is mandatory BEFORE a replacement can answer;
    # SO_REUSEPORT is never enabled, so a leaked old listener cannot be hidden.
    release = bind_listener(port)
    evidence.write("control-bind-release", release)
    assert release["released"] is True
    with socket.socket() as replacement:
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        replacement.bind(("127.0.0.1", port))
        replacement.listen(1)
        with running(replacement, second_identity):
            second = evidence.endpoint("known-replacement-server", port, second_identity)
            old = evidence.endpoint("old-identity-after-replacement", port, first_identity)
    after = evidence.endpoint("closed-control-endpoint", port, second_identity)
    closed_release = bind_listener(port)
    evidence.write("closed-control-bind-release", closed_release)
    evidence.write(
        "loopback-control",
        {
            "no_server": no_server,
            "first": known,
            "wrong": wrong,
            "release": release,
            "replacement": second,
            "old": old,
            "after": after,
            "bound_nonlistener_accept_without_reply": no_server["tcp_result"] == 0
            and not no_server["nonce_reply"],
            "after_close_accept_without_reply": after["tcp_result"] == 0
            and not after["nonce_reply"],
            "after_close_release": closed_release,
        },
    )
    assert no_server["nonce_reply"] is False
    assert known["nonce_reply"] is True and wrong["nonce_reply"] is False
    assert second["nonce_reply"] is True and old["nonce_reply"] is False
    assert after["nonce_reply"] is False
    assert closed_release["released"] is True
    # Record actual transport facts; do NOT assert a proxy cause or change any
    # guardian budget based on a source-only hypothesis.
