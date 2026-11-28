"""Portable pure controls for the crash fixture's exact retained-byte oracle.

These do not launch children, bind ports or claim kernel capability. Real crash,
identity, binding, keeper pinning and replacement controls remain mandatory.
"""

import copy
import errno
import json

import pytest

from tests.world4d_nonce_fixture import keeper_cleanup_proof


def event(operation, phase, *, children=0, terminal=0, status=True, error=0, deadline=20.0):
    return {
        "operation": operation,
        "phase": phase,
        "errno": error,
        "children": children,
        "terminal": terminal,
        "payload_status_present": status,
        "deadline": deadline,
    }


def prefix_events():
    return [
        event("admit", "starting", status=False, deadline=None),
        event("launch", "running", children=1, status=False, deadline=None),
    ]


def orphan_events():
    return prefix_events() + [
        event("failure", "failed", children=1, status=False, error=errno.EPIPE),
        event("group", "failed", children=1, status=False, error=errno.EPERM),
        event("child", "failed", children=1, status=False),
        event("observe", "failed", children=1, terminal=1),
        event("echild", "reap"),
        event("reap", "reap"),
    ]


def raw(events):
    return b"".join((json.dumps(item, sort_keys=True) + "\n").encode() for item in events)


def proof(events=None, **changes):
    values = dict(
        raw=raw(orphan_events() if events is None else events),
        pinned_prefix=raw(prefix_events()),
        same_file=True,
        posix=True,
        supervisor_returncode=-9,
        now=11.0,
        release_deadline=22.0,
    )
    values.update(changes)
    return keeper_cleanup_proof(**values)


@pytest.mark.parametrize(
    "group_errno", [errno.EPERM, errno.ESRCH], ids=["group-eperm", "group-esrch"]
)
def test_orphan_cleanup_keeps_parent_failure_distinct(group_errno):
    events = orphan_events()
    events[3]["errno"] = group_errno
    result = proof(events)
    assert result["proven"] and result["kernel_custody_drained"]
    assert result["classification"] == "orphan_cleanup_proven"
    assert result["status_channel_failed"] is True
    assert result["sticky_keeper_failed"] is True and result["parent_success_ack"] is False
    assert not any(item["operation"] == "drained" for item in events)


def test_normal_path_still_requires_its_complete_drained_suffix():
    events = prefix_events() + [
        event("stop", "term", children=1, status=False),
        event("echild", "reap"),
        event("reap", "reap"),
        event("drained", "drained"),
    ]
    result = proof(events, posix=False, supervisor_returncode=0)
    assert result["proven"] and result["classification"] == "normal_drained"
    assert result["status_channel_failed"] is False
    assert not proof(events[:-1], posix=False, supervisor_returncode=0)["proven"]
    unknown_failed_phase = copy.deepcopy(events)
    unknown_failed_phase[2]["phase"] = "failed"
    assert not proof(unknown_failed_phase, posix=False, supervisor_returncode=0)["proven"]
    failed = copy.deepcopy(events)
    failed.insert(3, event("failure", "failed", error=errno.EPIPE))
    assert not proof(failed)["proven"]


@pytest.mark.parametrize(
    "case",
    [
        "epipe-alone",
        "no-echild",
        "no-reap",
        "nonzero-children",
        "nonzero-terminal",
        "bool-children",
        "bool-terminal",
        "wrong-echild-phase",
        "wrong-reap-phase",
        "echild-error",
        "reap-error",
        "missing-payload-status",
        "numeric-payload-status",
        "unknown-failure",
        "zero-errno-failure",
        "two-failures",
        "observe-error",
        "child-send-error",
        "unknown-group-error",
        "telemetry-error",
        "unknown-operation",
        "missing-field",
        "extra-field",
        "non-object",
        "deadline-extended",
        "deadline-shortened",
        "deadline-missing-later",
        "deadline-nan",
        "deadline-infinite",
        "deadline-overflow",
        "deadline-bool",
        "send-after-reap",
        "observe-after-reap",
        "failure-after-reap",
        "extra-echild",
        "trailing-drained-after-failure",
        "failure-wrong-phase",
    ],
    ids=str,
)
def test_unknown_or_incomplete_custody_never_proves_orphan_cleanup(case):
    events = orphan_events()
    if case == "epipe-alone":
        events = events[:3]
    elif case == "no-echild":
        del events[-2]
    elif case == "no-reap":
        events.pop()
    elif case == "nonzero-children":
        events[-2]["children"] = 1
    elif case == "nonzero-terminal":
        events[-2].update(children=1, terminal=1)
    elif case == "bool-children":
        events[-2]["children"] = False
    elif case == "bool-terminal":
        events[-2]["terminal"] = False
    elif case == "wrong-echild-phase":
        events[-2]["phase"] = "failed"
    elif case == "wrong-reap-phase":
        events[-1]["phase"] = "failed"
    elif case == "echild-error":
        events[-2]["errno"] = errno.EIO
    elif case == "reap-error":
        events[-1]["errno"] = errno.ECHILD
    elif case == "missing-payload-status":
        events[-2]["payload_status_present"] = False
    elif case == "numeric-payload-status":
        events[-2]["payload_status_present"] = 1
    elif case == "unknown-failure":
        events[2]["errno"] = errno.EIO
    elif case == "zero-errno-failure":
        events[2]["errno"] = 0
    elif case == "two-failures":
        events.insert(3, copy.deepcopy(events[2]))
    elif case == "observe-error":
        events[5]["errno"] = errno.ECHILD
    elif case == "child-send-error":
        events[4]["errno"] = errno.ESRCH
    elif case == "unknown-group-error":
        events[3]["errno"] = errno.EIO
    elif case == "telemetry-error":
        events.insert(3, event("telemetry", "failed", error=errno.EIO))
    elif case == "unknown-operation":
        events[5]["operation"] = "unknown"
    elif case == "missing-field":
        del events[-2]["terminal"]
    elif case == "extra-field":
        events[-2]["untrusted"] = 0
    elif case == "non-object":
        events[-2] = []
    elif case == "deadline-extended":
        events[-1]["deadline"] = 21.0
    elif case == "deadline-shortened":
        events[-1]["deadline"] = 19.0
    elif case == "deadline-missing-later":
        events[-1]["deadline"] = None
    elif case == "deadline-nan":
        events[2]["deadline"] = float("nan")
    elif case == "deadline-infinite":
        events[2]["deadline"] = float("inf")
    elif case == "deadline-overflow":
        events[2]["deadline"] = 10**400
    elif case == "deadline-bool":
        events[2]["deadline"] = True
    elif case == "send-after-reap":
        events.insert(-1, event("child", "reap"))
    elif case == "observe-after-reap":
        events.append(event("observe", "reap"))
    elif case == "failure-after-reap":
        events.append(event("failure", "failed", error=errno.EPIPE))
    elif case == "extra-echild":
        events.insert(-2, event("echild", "reap"))
    elif case == "trailing-drained-after-failure":
        events.append(event("drained", "drained"))
    elif case == "failure-wrong-phase":
        events[2]["phase"] = "running"
    else:
        raise AssertionError("unhandled negative control")
    result = proof(events)
    assert result["proven"] is False and result["kernel_custody_drained"] is False
    assert result["classification"] == "unproven"


@pytest.mark.parametrize(
    "changes",
    [
        {"posix": False},
        {"supervisor_returncode": 0},
        {"supervisor_returncode": -15},
        {"supervisor_returncode": True},
        {"same_file": False},
        {"same_file": 1},
        {"pinned_prefix": b"different-file\n"},
        {"pinned_prefix": b""},
        {"now": 20.01},
        {"now": 22.01},
        {"release_deadline": 10.99},
        {"release_deadline": float("inf")},
        {"now": float("nan")},
        {"now": True},
    ],
    ids=[
        "nonposix",
        "normal-exit",
        "term-exit",
        "bool-exit",
        "wrong-inode",
        "numeric-pinning",
        "wrong-prefix",
        "empty-prefix",
        "keeper-deadline-expired",
        "both-deadlines-expired",
        "release-deadline-expired",
        "nonfinite-release",
        "nonfinite-clock",
        "bool-clock",
    ],
)
def test_crash_identity_pinning_and_both_original_deadlines_are_mandatory(changes):
    assert proof(**changes)["proven"] is False


@pytest.mark.parametrize(
    "case", ["truncated-line", "truncated-json", "bad-json", "duplicate-key", "invalid-utf8"]
)
def test_raw_receipt_corruption_is_not_cleanup_proof(case):
    receipt = raw(orphan_events())
    if case == "truncated-line":
        receipt = receipt[:-1]
    elif case == "truncated-json":
        receipt = receipt[:-3] + b"\n"
    elif case == "bad-json":
        receipt += b"not-json\n"
    elif case == "duplicate-key":
        head, last, ending = receipt.rsplit(b"\n", 2)
        receipt = b"\n".join(
            (head, last.replace(b'"errno": 0', b'"errno": 1, "errno": 0', 1), ending)
        )
    elif case == "invalid-utf8":
        receipt += b"\xff\n"
    assert not proof(raw=receipt)["proven"]
