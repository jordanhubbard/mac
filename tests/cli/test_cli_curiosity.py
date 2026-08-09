"""`mac curiosity list|approve|reject` — the hub-mediated quarantine verbs.

These exist because the curiosity ledger is unreachable from where it needs to
be used. The real CLI and its store live inside the ``mac-openclaw-<agent>``
sandbox; a dispatched task runs in a different ``mac-task-*`` sandbox and
cannot reach either. Every adjudication task ever filed against the quarantine
was therefore unsatisfiable, including ones correctly pinned to the owning host
(task_3a4503f0).

Routing through the hub is what makes adjudication possible from a task at all,
so these verbs are the agent-facing half of that fix.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.curiosity_service import CuriosityService, CuriosityUnavailable


def _run(tmp_path, *args, monkeypatch=None):
    from mac.test_support import dsn_for

    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def test_curiosity_list_reports_the_hosts_candidates(tmp_path, monkeypatch):
    payload = {
        "schema": "mac.curiosity_candidates.v1",
        "status": "quarantined",
        "count": 2,
        "candidates": [{"id": "cur_1"}, {"id": "cur_2"}],
    }
    monkeypatch.setattr(
        "mac.services.ControlPlane.list_curiosity_candidates",
        lambda self, status=None: payload,
    )

    rc, out = _run(tmp_path, "admin", "curiosity", "list", "--status", "quarantined")

    assert rc in (None, 0)
    assert out["count"] == 2
    assert [c["id"] for c in out["candidates"]] == ["cur_1", "cur_2"]


def test_curiosity_list_without_a_status_asks_for_everything(tmp_path, monkeypatch):
    seen = {}

    def _list(self, status=None):
        seen["status"] = status
        return {"schema": "mac.curiosity_candidates.v1", "count": 0, "candidates": []}

    monkeypatch.setattr(
        "mac.services.ControlPlane.list_curiosity_candidates", _list
    )

    rc, _out = _run(tmp_path, "admin", "curiosity", "list")

    assert rc in (None, 0)
    assert seen["status"] is None


@pytest.mark.parametrize("decision", ["approve", "reject"])
def test_curiosity_decision_carries_the_audit_trail(tmp_path, monkeypatch, decision):
    """actor/reason/approval-id are the external judgment the ledger requires."""
    seen = {}

    def _decide(self, candidate_id, verb, *, actor, reason, approval_id):
        seen.update(
            candidate_id=candidate_id,
            decision=verb,
            actor=actor,
            reason=reason,
            approval_id=approval_id,
        )
        return {"schema": "mac.curiosity_decision.v1", "decision": verb}

    monkeypatch.setattr(
        "mac.services.ControlPlane.decide_curiosity_candidate", _decide
    )

    rc, out = _run(
        tmp_path,
        "admin", "curiosity",
        decision,
        "cur_abc",
        "--actor",
        "agent_rocky",
        "--reason",
        "reproducible",
        "--approval-id",
        "task_123",
    )

    assert rc in (None, 0)
    assert out["decision"] == decision
    assert seen == {
        "candidate_id": "cur_abc",
        "decision": decision,
        "actor": "agent_rocky",
        "reason": "reproducible",
        "approval_id": "task_123",
    }


@pytest.mark.parametrize("omit", ["--actor", "--reason", "--approval-id"])
def test_a_decision_missing_an_audit_field_is_rejected_by_the_parser(tmp_path, omit):
    """The three fields are required at the CLI boundary, not just server-side.

    Dropping any one of them would let a promotion land without the external
    trail the quarantine design depends on.
    """
    args = [
        "admin", "curiosity",
        "approve",
        "cur_abc",
        "--actor",
        "a",
        "--reason",
        "r",
        "--approval-id",
        "t",
    ]
    index = args.index(omit)
    del args[index : index + 2]

    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, *args)
    assert excinfo.value.code != 0


def test_an_unknown_status_is_rejected_by_the_parser(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, "admin", "curiosity", "list", "--status", "bogus")
    assert excinfo.value.code != 0


def test_a_host_without_a_ledger_is_reported_as_unavailable(tmp_path):
    """Not every host runs an OpenClaw gateway; that is not a fault."""
    service = CuriosityService.__new__(CuriosityService)
    from mac.curiosity_service import CuriosityConfig

    service.config = CuriosityConfig(wrapper_path=tmp_path / "nope")
    service._runner = None
    with pytest.raises(CuriosityUnavailable):
        service.list_candidates()
