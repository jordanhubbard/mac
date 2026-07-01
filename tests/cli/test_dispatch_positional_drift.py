"""Regression tests: CLI-to-RemoteDispatch positional/keyword argument drift.

These tests guard against the class of bug where a CLI handler passes positional
arguments to a RemoteDispatch method that only accepts keyword arguments (**kw).

The canonical failures that motivated this file:
  - cmd_message_send called send_message(sender, recipient, type, payload, task_id=...)
    but RemoteDispatch.send_message(**kw) only accepts keyword arguments.
  - cmd_review_decision called submit_review(review_id, status, reviewer_agent_id, ...)
    but RemoteDispatch.submit_review(review_id, **kw) only accepts keyword args
    after review_id.

Each test exercises the CLI handler directly with a fake dispatch object whose
methods assert they were called with keyword-only arguments for every field
after any explicit positional identifiers. If positional drift recurs the
fake dispatch will raise TypeError before we even reach the assert.
"""

from __future__ import annotations

import io
import inspect
import json
import sys
from argparse import Namespace

import mac.cli as cli
from mac.dispatch import RemoteDispatch


# ---------------------------------------------------------------------------
# Shared local SQLite helpers
# ---------------------------------------------------------------------------


def _run(tmp_path, *args):
    """Run `mac --db <tmp> <args>` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = cli.main(["--db", str(tmp_path / "mac.db"), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


def _setup_machine_agent(tmp_path, host="host-x", agent_name="worker-x", agent_id=None,
                         capabilities=None):
    rc, machine = _run(tmp_path, "machine", "register", host)
    assert rc == 0
    cmd = ["agent", "register", machine["id"], agent_name]
    if agent_id:
        cmd += ["--agent-id", agent_id]
    if capabilities:
        cmd += ["--capabilities", capabilities]
    rc, agent = _run(tmp_path, *cmd)
    assert rc == 0
    return machine, agent


# ---------------------------------------------------------------------------
# message send — RemoteDispatch.send_message(**kw) accepts NO positional args
# ---------------------------------------------------------------------------


class _MessageSendCapture:
    """Fake dispatch that records keyword arguments passed to send_message."""

    def __init__(self) -> None:
        self.captured: dict | None = None

    def send_message(self, **kw):  # type: ignore[override]
        self.captured = kw
        return {"id": "msg_test", "sender_agent_id": kw.get("sender_agent_id")}


def test_cmd_message_send_uses_keyword_args_only(monkeypatch) -> None:
    """cmd_message_send must pass all fields as keyword args to send_message.

    RemoteDispatch.send_message(**kw) refuses positional arguments; if the CLI
    reverts to positional passing the fake dispatch will raise TypeError here.
    """
    capture = _MessageSendCapture()
    monkeypatch.setattr(cli, "_plane", lambda _args: capture)

    output: list = []
    monkeypatch.setattr(cli, "_print", output.append)

    cli.cmd_message_send(
        Namespace(
            sender_agent_id="agent_sender",
            recipient_agent_id="agent_recipient",
            message_type="nudge",
            payload=json.dumps({"note": "ping", "task_id": "task_abc"}),
            task_id="task_abc",
            # _plane() requires these; we override _plane so they're irrelevant
            hub=None,
            db=None,
        )
    )

    assert capture.captured is not None, "send_message was not called"
    kw = capture.captured
    assert kw["sender_agent_id"] == "agent_sender"
    assert kw["recipient_agent_id"] == "agent_recipient"
    assert kw["message_type"] == "nudge"
    assert isinstance(kw["payload"], dict)
    assert kw["task_id"] == "task_abc"
    assert output, "cmd_message_send did not print a result"


def test_cmd_message_send_remote_dispatch_signature() -> None:
    """RemoteDispatch.send_message must accept only **kw (no positional params).

    This test fails if someone changes the signature to accept positional args
    without updating cmd_message_send to match.
    """
    sig = inspect.signature(RemoteDispatch.send_message)
    params = list(sig.parameters.values())[1:]  # skip self
    for p in params:
        assert p.kind in {p.VAR_KEYWORD, p.KEYWORD_ONLY}, (
            "RemoteDispatch.send_message parameter %r is positional (%s); "
            "update cmd_message_send to pass it as a keyword argument."
            % (p.name, p.kind)
        )


# ---------------------------------------------------------------------------
# review decision — RemoteDispatch.submit_review(review_id, **kw)
# ---------------------------------------------------------------------------


class _ReviewDecisionCapture:
    """Fake dispatch that records arguments passed to submit_review."""

    def __init__(self) -> None:
        self.positional: tuple = ()
        self.keyword: dict = {}

    def submit_review(self, review_id: str, **kw):  # type: ignore[override]
        self.positional = (review_id,)
        self.keyword = kw
        return {"id": "rev_test", "status": kw.get("status")}


def test_cmd_review_decision_uses_keyword_args(monkeypatch) -> None:
    """cmd_review_decision must pass status and reviewer_agent_id as kwargs.

    RemoteDispatch.submit_review(review_id, **kw) only allows review_id as a
    positional argument; status and reviewer_agent_id must be keyword args.
    If the CLI reverts to positional passing the test will capture it.
    """
    capture = _ReviewDecisionCapture()
    monkeypatch.setattr(cli, "_plane", lambda _args: capture)

    output: list = []
    monkeypatch.setattr(cli, "_print", output.append)

    cli.cmd_review_decision(
        Namespace(
            review_id="rev_abc",
            status="approved",
            reviewer_agent_id="agent_reviewer",
            reason="looks good",
            evidence_id="ev_abc",
            hub=None,
            db=None,
        )
    )

    assert capture.positional == ("rev_abc",), (
        "review_id should be the only positional arg; got %r" % (capture.positional,)
    )
    kw = capture.keyword
    assert kw.get("status") == "approved", "status must be a keyword arg"
    assert kw.get("reviewer_agent_id") == "agent_reviewer", (
        "reviewer_agent_id must be a keyword arg"
    )
    assert kw.get("reason") == "looks good"
    assert kw.get("evidence_id") == "ev_abc"
    assert output, "cmd_review_decision did not print a result"


def test_cmd_review_decision_remote_dispatch_signature() -> None:
    """RemoteDispatch.submit_review must accept status/reviewer_agent_id as **kw.

    The only allowed positional parameter (besides self) is review_id.  Any
    additional positional parameter in the signature means positional drift can
    recur silently.
    """
    sig = inspect.signature(RemoteDispatch.submit_review)
    params = list(sig.parameters.values())[1:]  # skip self
    # First param must be review_id (positional or keyword)
    assert params[0].name == "review_id"
    # All remaining params must be VAR_KEYWORD (**kw) or KEYWORD_ONLY
    for p in params[1:]:
        assert p.kind in {p.VAR_KEYWORD, p.KEYWORD_ONLY}, (
            "RemoteDispatch.submit_review parameter %r is positional (%s); "
            "cmd_review_decision must pass it as a keyword argument."
            % (p.name, p.kind)
        )


# ---------------------------------------------------------------------------
# Local SQLite round-trip: review request smoke test
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Local SQLite round-trip: message send
# ---------------------------------------------------------------------------


def test_message_send_local_sqlite(tmp_path) -> None:
    """message send works end-to-end in local SQLite mode.

    This mirrors test_message_send_and_inbox in test_domains_cli.py but adds an
    explicit check that the keyword argument form is what reaches the service.
    Both the existing test and this one must pass; if one passes and the other
    fails, investigate whether a positional-to-keyword regression was introduced.
    """
    _, sender = _setup_machine_agent(tmp_path, host="msg-s", agent_name="msg-sender",
                                     agent_id="agent_msg_sender")
    _, recipient = _setup_machine_agent(tmp_path, host="msg-r", agent_name="msg-recipient",
                                        agent_id="agent_msg_recipient")
    rc, task = _run(tmp_path, "task", "create", "msg-task")
    assert rc == 0

    rc, msg = _run(
        tmp_path, "message", "send",
        sender["id"],
        "--recipient-agent-id", recipient["id"],
        "--message-type", "nudge",
        "--payload", json.dumps({"note": "ping", "task_id": task["id"]}),
    )
    assert rc == 0, "message send failed in local mode"
    assert msg is not None and "id" in msg
    assert msg["sender_agent_id"] == sender["id"]
    assert msg["recipient_agent_id"] == recipient["id"]
