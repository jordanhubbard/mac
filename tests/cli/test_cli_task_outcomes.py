"""Exercise the user handoff through the parser and real outcome owner."""

import json

from mac import cli
from mac.services import ControlPlane
from tests.test_task_outcomes import _reviewing


def _run(capsys, *args):
    rc = cli.main(["--json", *args])
    assert rc in (None, 0)
    return json.loads(capsys.readouterr().out)


def test_cli_outcome_accept_and_creation_cohort(monkeypatch, capsys, tmp_path):
    cp = ControlPlane.in_memory()
    tid, eid = _reviewing(cp)
    monkeypatch.setattr(cli, "_plane", lambda args: cp)
    reason = tmp_path / "acceptance.txt"
    reason.write_text("Observed the requested behavior; $HOME and `commands` are literal.\n")
    before = _run(capsys, "task", "outcome", tid)
    assert before["acceptance"]["status"] == "unknown"
    accepted = _run(capsys, "task", "accept", tid, "--evidence", eid, "--reason-file", str(reason))
    assert accepted["acceptance"]["reason"] == reason.read_text().strip()
    assert accepted["acceptance"]["status"] == "accepted"
    rejected = _run(
        capsys, "task", "accept", tid, "--evidence", eid, "--reason", "Scenario fails", "--reject"
    )
    assert rejected["acceptance"]["status"] == "rejected"
    cohort = _run(
        capsys, "task", "outcomes", "--project", "trust", "--since-hours", "24", "--limit", "1"
    )
    assert cohort["count"] == 1
    assert cohort["tasks"][0]["accepted"] is False
