"""Incidental PR references must not authorize task or branch retirement."""

from types import SimpleNamespace

import pytest

from mac.judgement import JudgementConfig, JudgementProcess
from mac.services import ControlPlane


@pytest.fixture
def cp():
    return ControlPlane.in_memory()


def _task(cp, title, state="open"):
    task = cp.create_task(title, project="mac")
    with cp.store.transaction() as conn:
        conn.execute("UPDATE tasks SET state = ? WHERE id = ?", (state, task.id))
    return task


def _findings(cp, open_prs=(), merged_prs=()):
    process = JudgementProcess(
        cp,
        JudgementConfig(enabled=True),
        pr_lister=lambda _root: {"open": list(open_prs), "merged": list(merged_prs)},
    )
    return process._check_orphaned_pull_requests()


@pytest.mark.parametrize(
    "body",
    [
        "Task: {owner}. Investigation: {report}.",
        "Task: {owner}\nInvestigation: {report}",
        "Task ID: `{owner}`\nSee {report} for evidence.",
        "Task: {owner}\nPrevious failed attempt: {report}",
    ],
)
def test_completed_reference_does_not_orphan_live_implementation(cp, body):
    owner = _task(cp, "implementation", "reviewing")
    report = _task(cp, "investigation", "completed")
    assert (
        _findings(cp, [{"number": 808, "body": body.format(owner=owner.id, report=report.id)}])
        == []
    )


def test_merged_pr_reference_does_not_mean_referenced_work_landed(cp):
    owner = _task(cp, "unpublished implementation", "reviewing")
    other = _task(cp, "different published work", "completed")
    assert (
        _findings(
            cp,
            [{"number": 808, "body": "Task: " + owner.id}],
            [{"number": 801, "title": other.id, "body": "Related future work: " + owner.id}],
        )
        == []
    )


def test_shared_investigation_does_not_make_independent_prs_duplicates(cp):
    report = _task(cp, "shared investigation")
    owners = [_task(cp, "first"), _task(cp, "second")]
    prs = [
        {"number": number, "body": f"Task: {owner.id}\nInvestigation: {report.id}"}
        for number, owner in enumerate(owners, 808)
    ]
    assert _findings(cp, prs) == []


def test_body_reference_without_ownership_cannot_authorize_cleanup(cp):
    report = _task(cp, "completed reference", "completed")
    assert _findings(cp, [{"number": 808, "body": "Investigation: " + report.id}]) == []


def test_same_explicit_owner_still_identifies_a_duplicate(cp):
    owner = _task(cp, "live implementation", "reviewing")
    findings = _findings(
        cp, [{"number": number, "body": "Task: " + owner.id} for number in [808, 809]]
    )
    assert [(f.kind, f.task_id, f.detail["pr_number"]) for f in findings] == [
        ("duplicate_pull_request", owner.id, 808)
    ]


@pytest.mark.parametrize("suffix", ["_notes", "z", "a"])
def test_task_id_substring_in_a_longer_identifier_is_not_ownership(cp, suffix):
    owner = _task(cp, "completed owner", "completed")
    assert _findings(cp, [{"number": 808, "title": owner.id + suffix}]) == []


def test_merged_task_reconciliation_excludes_same_line_investigation(cp):
    owner = _task(cp, "merged implementation")
    report = _task(cp, "uncompleted report")
    findings = _findings(
        cp,
        merged_prs=[{"number": 808, "body": f"Task: {owner.id}. Investigation: {report.id}."}],
    )
    assert [(f.kind, f.task_id) for f in findings] == [("merged_task_not_reconciled", owner.id)]


@pytest.mark.parametrize("where", ["title", "body", "branch", "comma"])
def test_ambiguous_ownership_cannot_close_pr_or_complete_either_task(cp, where):
    first = _task(cp, "first")
    second = _task(cp, "second")
    pr = {"number": 808, "body": "Task: " + first.id}
    if where == "title":
        pr["title"] = "Implement " + second.id
    elif where == "branch":
        pr["headRefName"] = "mac/" + second.id
    elif where == "comma":
        pr["body"] += ", " + second.id
    else:
        pr["body"] += "\nTask: " + second.id
    assert _findings(cp, merged_prs=[pr]) == []
    for task in [first, second]:
        with cp.store.transaction() as conn:
            conn.execute("UPDATE tasks SET state = 'completed' WHERE id = ?", (task.id,))
    assert _findings(cp, [pr]) == []


@pytest.mark.parametrize("where", ["title", "branch", "body", "bare_body"])
def test_unambiguous_owning_task_still_authorizes_orphan_cleanup(cp, where):
    owner = _task(cp, "completed owner", "completed")
    pr = {"number": 808}
    if where == "title":
        pr["title"] = "Implementation (" + owner.id + ")"
    elif where == "branch":
        pr["headRefName"] = "mac/" + owner.id
    else:
        pr["body"] = ("Task: " if where == "body" else "") + owner.id
    findings = _findings(cp, [pr])
    assert [(f.kind, f.task_id) for f in findings] == [("orphaned_pull_request", owner.id)]


def test_owning_full_id_and_its_short_prefix_identify_one_task(cp):
    owner = _task(cp, "completed owner", "completed")
    pr = {"number": 808, "title": owner.id[:13], "body": "Task: " + owner.id}
    findings = _findings(cp, [pr])
    assert [(f.kind, f.task_id) for f in findings] == [("orphaned_pull_request", owner.id)]


@pytest.mark.parametrize("matching", [1, 2])
def test_prefix_resolution_requires_exactly_one_known_task(cp, monkeypatch, matching):
    tasks = [SimpleNamespace(id="task_12345678" + str(i) * 24) for i in range(matching)]
    process = JudgementProcess(cp, JudgementConfig(enabled=True))
    monkeypatch.setattr(process, "_all_known_tasks", lambda: tasks)
    result = process._resolve_task("task_12345678")
    assert result == (tasks[0] if matching == 1 else None)


def test_unknown_full_id_cannot_resolve_to_another_task_with_same_prefix(cp, monkeypatch):
    existing = SimpleNamespace(id="task_12345678" + "a" * 24)
    process = JudgementProcess(cp, JudgementConfig(enabled=True))
    monkeypatch.setattr(process, "_all_known_tasks", lambda: [existing])
    assert process._resolve_task("task_12345678" + "b" * 24) is None
