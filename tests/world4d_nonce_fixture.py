"""Stdlib-only application identity fixture, also loaded by owned test payloads.

A TCP handshake is recorded, never equated with an application listener. The
request contains only a fresh challenge; a matching reply also requires the
server's separately assigned identity, which a transparent acceptor cannot
obtain by echoing the request. These are synthetic test IDs, not credentials.
"""

import errno
import json
import math
import os
import select
import socket
import time
import uuid

REQUEST = b"world4d-fixture-request/v1 "
REPLY = b"world4d-fixture-reply/v1 "
LIMIT = 128
PROBE_SECONDS = 0.2


def _line(connection, deadline):
    data = bytearray()
    while not data.endswith(b"\n"):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError()
        connection.settimeout(remaining)
        part = connection.recv(LIMIT + 1 - len(data))
        if not part:
            return bytes(data)
        data.extend(part)
        if len(data) > LIMIT:
            return bytes(data)
    return bytes(data)


def probe(port, identity):
    """Bounded real transport observation; caller asserts its intended meaning."""
    challenge = uuid.uuid4().hex.encode("ascii")
    expected = REPLY + identity.encode("ascii") + b" " + challenge + b"\n"
    result = {"tcp_result": None, "nonce_reply": False, "outcome": "connect_error", "errno": None}
    deadline = time.monotonic() + PROBE_SECONDS
    with socket.socket() as connection:
        connection.settimeout(PROBE_SECONDS)
        try:
            result["tcp_result"] = connection.connect_ex(("127.0.0.1", port))
            if result["tcp_result"]:
                result["errno"] = result["tcp_result"]
                return result
            connection.settimeout(max(0.001, deadline - time.monotonic()))
            connection.sendall(REQUEST + challenge + b"\n")
            response = _line(connection, deadline)
            result["nonce_reply"] = response == expected
            result["outcome"] = "matched" if result["nonce_reply"] else "no_match"
        except TimeoutError:
            result["outcome"] = "timeout"
        except OSError as exc:
            result["outcome"] = "socket_error"
            result["errno"] = exc.errno if type(exc.errno) is int else None
    return result


def serve(listener, identity, deadline, *, stop_reader=None, stop_event=None):
    """Serve one exact identity until an existing autonomous/EOF deadline."""
    watched = [listener] + ([] if stop_reader is None else [stop_reader])
    while time.monotonic() < deadline and not (stop_event and stop_event.is_set()):
        ready, _, _ = select.select(watched, [], [], min(0.05, max(0, deadline - time.monotonic())))
        if stop_reader is not None and stop_reader in ready:
            if not os.read(stop_reader.fileno(), 1):
                return
        if listener not in ready:
            continue
        connection, _address = listener.accept()
        with connection:
            try:
                request = _line(connection, min(deadline, time.monotonic() + PROBE_SECONDS))
                challenge = (
                    request[len(REQUEST) : -1]
                    if request.startswith(REQUEST) and request.endswith(b"\n")
                    else b""
                )
                if len(challenge) != 32 or any(c not in b"0123456789abcdef" for c in challenge):
                    continue
                connection.settimeout(max(0.001, min(PROBE_SECONDS, deadline - time.monotonic())))
                connection.sendall(REPLY + identity.encode("ascii") + b" " + challenge + b"\n")
            except (OSError, TimeoutError):
                # A diagnostic TCP-only connect/close is deliberately not a
                # valid identity request. It must not kill the fixture server.
                continue


def bind_listener(port):
    """Actual kernel release proof: no SO_REUSEPORT and a real listen call."""
    with socket.socket() as replacement:
        replacement.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            replacement.bind(("127.0.0.1", port))
            replacement.listen(1)
        except OSError as exc:
            return {"released": False, "errno": exc.errno if type(exc.errno) is int else errno.EIO}
    return {"released": True, "errno": None}


def keeper_cleanup_proof(
    raw, *, pinned_prefix, same_file, posix, supervisor_returncode, now, release_deadline
):
    """Pure test-only proof; orphan cleanup NEVER upgrades a failed parent API.

    Consume exact retained bytes, not inferred process state. Only the deliberate
    POSIX SIGKILL fixture can recognize the EPIPE orphan branch. Normal live-parent
    success retains its distinct full DRAINED suffix and no sticky failure.
    """
    result = {
        "proven": False,
        "status_channel_failed": None,
        "kernel_custody_drained": False,
        "classification": "unproven",
        "deadline": None,
    }

    def number(value):
        try:
            return type(value) in (int, float) and math.isfinite(value) and value > 0
        except OverflowError:
            return False

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate fixture evidence key")
            value[key] = item
        return value

    if (
        same_file is not True
        or type(raw) is not bytes
        or type(pinned_prefix) is not bytes
        or not pinned_prefix
        or not raw.startswith(pinned_prefix)
        or not raw.endswith(b"\n")
        or len(raw) > 2 * 1024 * 1024
        or not number(now)
        or not number(release_deadline)
        or now > release_deadline
    ):
        return result
    try:
        events = [json.loads(line, object_pairs_hook=unique_object) for line in raw.splitlines()]
    except (ValueError, UnicodeError):
        return result
    keys = {
        "phase",
        "operation",
        "errno",
        "children",
        "terminal",
        "deadline",
        "payload_status_present",
    }
    phases = {"starting", "running", "term", "kill", "failed", "reap", "drained"}
    operations = {
        "admit",
        "launch",
        "ready",
        "eof",
        "stop",
        "observe",
        "group",
        "child",
        "failure",
        "reap",
        "echild",
        "drained",
    }
    deadline = None
    for event in events:
        if (
            type(event) is not dict
            or set(event) != keys
            or type(event["phase"]) is not str
            or event["phase"] not in phases
            or type(event["operation"]) is not str
            or event["operation"] not in operations
            or type(event["errno"]) is not int
            or not 0 <= event["errno"] <= 4095
            or type(event["children"]) is not int
            or event["children"] < 0
            or type(event["terminal"]) is not int
            or not 0 <= event["terminal"] <= event["children"]
            or type(event["payload_status_present"]) is not bool
        ):
            return result
        current = event["deadline"]
        if current is not None:
            if not number(current) or (deadline is not None and current != deadline):
                return result
            deadline = current
        elif deadline is not None:
            return result
        # Known group refusal/race is fallback evidence only. Every unknown
        # observation, reap, telemetry or direct-child-send error fails closed.
        if event["errno"] and not (
            event["operation"] == "group"
            and event["errno"] in (errno.EPERM, errno.ESRCH)
            or event["operation"] == "failure"
            and event["errno"] == errno.EPIPE
        ):
            return result
    result["deadline"] = deadline
    failures = [event for event in events if event["operation"] == "failure"]
    status_failed = (
        len(failures) == 1
        and failures[0]["phase"] == "failed"
        and failures[0]["errno"] == errno.EPIPE
    )
    result["status_channel_failed"] = status_failed
    failure_index = next(
        (i for i, event in enumerate(events) if event["operation"] == "failure"), None
    )
    if any(
        event["phase"] == "failed"
        and (not status_failed or failure_index is None or i < failure_index)
        for i, event in enumerate(events)
    ):
        return result
    if deadline is None or now > deadline:
        return result
    normal = len(events) >= 3 and [(e["operation"], e["phase"]) for e in events[-3:]] == [
        ("echild", "reap"),
        ("reap", "reap"),
        ("drained", "drained"),
    ]
    orphan = (
        posix is True
        and type(supervisor_returncode) is int
        and supervisor_returncode == -9
        and status_failed
        and len(events) >= 2
        and [(e["operation"], e["phase"]) for e in events[-2:]]
        == [("echild", "reap"), ("reap", "reap")]
    )
    if normal and not failures:
        final = events[-3:]
        prefix = events[:-3]
        classification = "normal_drained"
    elif orphan:
        final = events[-2:]
        prefix = events[:-2]
        classification = "orphan_cleanup_proven"
    else:
        return result
    if any(
        event["operation"] in {"echild", "reap", "drained"} or event["phase"] in {"reap", "drained"}
        for event in prefix
    ):
        return result  # no sends, observations or other records after reaping starts
    if any(
        event["errno"] != 0
        or event["children"] != 0
        or event["terminal"] != 0
        or event["payload_status_present"] is not True
        for event in final
    ):
        return result
    result.update(
        proven=True, kernel_custody_drained=True, classification=classification, final=final
    )
    if classification == "orphan_cleanup_proven":
        result.update(sticky_keeper_failed=True, parent_success_ack=False)
    return result
