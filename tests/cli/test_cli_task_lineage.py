"""`mac task lineage` and `mac task reopen --replace`, driven through the CLI.

The control-plane behaviour is covered in tests/test_terminal_evidence_claim_gate.py
and tests/test_task_lineage.py. This file exists because the operator-facing
half is a separate contract: the whole point of lineage is that a human (or an
agent about to re-implement something) can ask "what replaced this, and what
did it replace?" from a terminal, and get an answer instead of free text.

Lives in tests/cli/ rather than at the top level of tests/ for the reason
tests/cli/conftest.py documents: it drives the CLI through ``main()`` so the
CLI coverage gate sees it, and the conftest supplies the MAC_SECRET_KEY the
CLI needs under ``run-contract-tests.sh``, which sweeps MAC_* for hermeticity.
"""

from __future__ import annotations

import io
import json
import sys

from mac.cli import main
from mac.test_support import control_plane_on, dsn_for


MERGED_PR = "https://github.com/example/mac/pull/498"


def _run(tmp_path, *args):
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--db", dsn_for(tmp_path), "--json", *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, (json.loads(raw) if raw else None)


def _merged_pull_request_evidence(cp, task_id):
    return cp.add_evidence(
        task_id,
        "repo_change",
        "https://example.invalid/evidence",
        "work landed as a merged pull request",
        "human",
        metadata={
            "verification": {
                "repo": {
                    "pull_request": {
                        "merged": True,
                        "number": 498,
                        "url": MERGED_PR,
                    }
                }
            }
        },
        _trusted_internal=True,
    )


def test_lineage_of_an_untouched_task_is_empty_rather_than_an_error(tmp_path):
    """The common case has to answer, not raise: absence of lineage is a fact."""
    cp = control_plane_on(dsn_for(tmp_path))
    task = cp.create_task(title="implement the module", description="d")

    rc, payload = _run(tmp_path, "task", "lineage", task.id)

    assert rc in (None, 0)
    assert payload["task_id"] == task.id
    assert payload["replaces"] == []
    assert payload["replaced_by"] == []
    assert payload["terminal_evidence"]["present"] is False


def test_reopen_replace_then_lineage_answers_both_directions(tmp_path):
    """The operator path out of the duplicate-PR incident, end to end.

    A merged row is reopened with --replace, which prints the replacement
    rather than the original; `task lineage` on either id then names the other.
    """
    cp = control_plane_on(dsn_for(tmp_path))
    task = cp.create_task(title="implement the module", description="d")
    _merged_pull_request_evidence(cp, task.id)

    rc, replacement = _run(
        tmp_path, "task", "reopen", task.id, "--replace", "--reason", "fleet restart"
    )
    assert rc in (None, 0)
    replacement_id = replacement["id"]
    assert replacement_id != task.id

    rc, prior_view = _run(tmp_path, "task", "lineage", task.id)
    assert rc in (None, 0)
    assert prior_view["replaces"] == []
    assert [
        (entry["relation"], entry["source"]) for entry in prior_view["replaced_by"]
    ] == [("retried_by", {"kind": "task", "ref": replacement_id})]
    # The prior row is still the one carrying terminal evidence, which is what
    # keeps it non-claimable while its replacement is claimable.
    assert prior_view["terminal_evidence"]["present"] is True
    assert "/pull/498" in prior_view["terminal_evidence"]["summary"]

    rc, successor_view = _run(tmp_path, "task", "lineage", replacement_id)
    assert rc in (None, 0)
    assert successor_view["replaced_by"] == []
    assert successor_view["replaces"][0]["target"] == {"kind": "task", "ref": task.id}


def test_reopen_without_replace_refuses_and_names_the_evidence(tmp_path, capsys):
    """Without --replace the CLI must refuse, not silently re-queue the row.

    The refusal is the operator-visible half of the gate, so it has to name the
    evidence and the way forward -- "denied" alone would just get retried.
    """
    cp = control_plane_on(dsn_for(tmp_path))
    task = cp.create_task(title="implement the module", description="d")
    _merged_pull_request_evidence(cp, task.id)
    before = cp.get_task(task.id).state

    rc, _ = _run(tmp_path, "task", "reopen", task.id, "--reason", "fleet restart")

    assert rc not in (None, 0)
    message = capsys.readouterr().err
    assert "refusing to reopen" in message
    assert "/pull/498" in message
    assert "--replace" in message
    # The row must not have been re-queued on the way to refusing.
    assert cp.get_task(task.id).state == before


def test_lineage_projects_a_pull_request_supersession(tmp_path):
    """Cancelling as superseded by a merged PR is queryable, not free text."""
    cp = control_plane_on(dsn_for(tmp_path))
    task = cp.create_task(title="implement the module", description="d")

    rc, _ = _run(
        tmp_path,
        "task",
        "cancel",
        task.id,
        "--reason",
        "the work landed as a merged pull request",
        "--disposition",
        "superseded",
        "--replacement-pull-request",
        MERGED_PR,
    )
    assert rc in (None, 0)

    rc, view = _run(tmp_path, "task", "lineage", task.id)
    assert rc in (None, 0)
    sources = [entry["source"] for entry in view["replaced_by"]]
    assert {"kind": "pull_request", "ref": MERGED_PR} in [
        {"kind": source["kind"], "ref": source["ref"]} for source in sources
    ]
