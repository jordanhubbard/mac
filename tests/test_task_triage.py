"""Triage a task against its branch head before working it.

The headline test is ``test_a_pull_request_that_merely_mentions_the_task_does_not_close_it``
and its siblings: the naive signal ("a merged change references this task id")
returned 24 hits across 75 held tasks and every one was a false positive, so
the mention/citation distinction is pinned from several directions -- the
decision, the constructor, and the close gate.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mac.task_triage import (
    ChangeRef,
    ChangeRelation,
    TriageAction,
    TriageBudget,
    TriageCost,
    TriageDecision,
    TriageError,
    TriageEvidence,
    TriageOutcome,
    TriageReason,
    acceptance_markers_from_task,
    collect_triage_evidence,
    decide_triage,
    ledger_payload,
    plan_scope_update,
    scope_paths_from_task,
    validate_close,
)

TASK_ID = "task_9f3b80b8c1d24e0f"


def _task(**overrides):
    task = {
        "id": TASK_ID,
        "title": "Add the widget",
        "description": "Change `src/mac/widget.py` so the widget exists.",
        "metadata": {},
    }
    task.update(overrides)
    return task


def _evidence(**overrides) -> TriageEvidence:
    base = {
        "task_id": TASK_ID,
        "repository": "mac",
        "branch": "main",
        "head_sha": "a" * 40,
        "scope_paths": ("src/mac/widget.py",),
        "present_paths": ("src/mac/widget.py",),
        "missing_paths": (),
        "acceptance_markers": ("def build_widget(",),
        "markers_present": ("def build_widget(",),
        "markers_absent": (),
        "candidates": (),
    }
    base.update(overrides)
    return TriageEvidence(**base)


def _implementing(ref: str = "b" * 40) -> ChangeRef:
    return ChangeRef(
        kind="commit",
        ref=ref,
        subject="add the widget",
        relation=ChangeRelation.IMPLEMENTS_SCOPE,
        matched_paths=("src/mac/widget.py",),
    )


def _mention(ref: str = "c" * 40) -> ChangeRef:
    return ChangeRef(
        kind="pull_request",
        ref="#489",
        subject="ADR 0018: task-graph progressive disclosure (cites %s)" % TASK_ID,
        relation=ChangeRelation.MENTIONS_TASK_ID,
    )


# --- a mention is not a citation -------------------------------------------


def test_a_pull_request_that_merely_mentions_the_task_does_not_close_it():
    """PR #489 names the task because an ADR quotes it as motivating evidence.

    This is the exact live false positive: closing on it would have destroyed
    a task that was entirely outstanding.
    """
    decision = decide_triage(
        _evidence(
            candidates=(_mention(),),
            markers_present=(),
            markers_absent=("def build_widget(",),
        )
    )
    assert decision.outcome == TriageOutcome.STILL_NEEDED
    assert decision.reason == TriageReason.MENTION_IS_NOT_A_CITATION
    assert decision.action == TriageAction.PROCEED
    assert decision.closes is False
    assert decision.citation is None


def test_many_mentions_still_do_not_close_a_task():
    """24 false positives were 24 mentions. Volume is not evidence."""
    mentions = tuple(
        ChangeRef(
            kind="pull_request",
            ref="#%d" % (480 + index),
            subject="cites %s" % TASK_ID,
            relation=ChangeRelation.MENTIONS_TASK_ID,
        )
        for index in range(24)
    )
    decision = decide_triage(
        _evidence(
            candidates=mentions,
            markers_present=(),
            markers_absent=("def build_widget(",),
        )
    )
    assert decision.outcome == TriageOutcome.STILL_NEEDED
    assert decision.reason == TriageReason.MENTION_IS_NOT_A_CITATION


def test_a_closing_verdict_cannot_be_constructed_from_a_mention():
    """The guard is structural, not a convention the caller must remember."""
    with pytest.raises(TriageError) as excinfo:
        TriageDecision(
            outcome=TriageOutcome.ALREADY_LANDED,
            reason=TriageReason.LANDED_CITED_CHANGE,
            evidence=_evidence(),
            citation=_mention(),
        )
    assert "mention is not a citation" in str(excinfo.value)


def test_a_closing_verdict_cannot_be_constructed_without_any_citation():
    with pytest.raises(TriageError):
        TriageDecision(
            outcome=TriageOutcome.SUPERSEDED,
            reason=TriageReason.SUPERSEDED_BY_CITED_CHANGE,
            evidence=_evidence(),
            citation=None,
        )


def test_validate_close_refuses_a_non_closing_verdict():
    decision = decide_triage(_evidence(candidates=(), markers_absent=("x",)))
    with pytest.raises(TriageError):
        validate_close(decision)


# --- the five verdicts -----------------------------------------------------


def test_already_landed_needs_both_a_change_and_every_marker():
    decision = decide_triage(_evidence(candidates=(_implementing(),)))
    assert decision.outcome == TriageOutcome.ALREADY_LANDED
    assert decision.reason == TriageReason.LANDED_CITED_CHANGE
    assert decision.action == TriageAction.CLOSE
    assert validate_close(decision).ref == "b" * 40
    assert "b" * 12 in decision.summary or decision.citation.ref == "b" * 40


def test_a_change_touching_the_scope_with_markers_absent_is_not_landed():
    """Someone edited the file. That is not the same as the work being done."""
    decision = decide_triage(
        _evidence(
            candidates=(_implementing(),),
            markers_present=(),
            markers_absent=("def build_widget(",),
        )
    )
    assert decision.outcome == TriageOutcome.STILL_NEEDED
    assert decision.closes is False


def test_partially_present_markers_are_cannot_tell_not_landed():
    decision = decide_triage(
        _evidence(
            acceptance_markers=("def build_widget(", "widget_cli"),
            markers_present=("def build_widget(",),
            markers_absent=("widget_cli",),
            candidates=(_implementing(),),
        )
    )
    assert decision.outcome == TriageOutcome.CANNOT_TELL
    assert decision.reason == TriageReason.PARTIAL_ACCEPTANCE_EVIDENCE
    assert decision.action == TriageAction.PROCEED


def test_superseded_cites_the_change_that_replaced_the_work():
    replacement = ChangeRef(
        kind="commit",
        ref="d" * 40,
        subject="rewrite widgets; supersedes %s" % TASK_ID,
        relation=ChangeRelation.REPLACES_SCOPE,
    )
    decision = decide_triage(
        _evidence(candidates=(_mention(), replacement), markers_absent=("x",))
    )
    assert decision.outcome == TriageOutcome.SUPERSEDED
    assert decision.action == TriageAction.CLOSE
    assert validate_close(decision).ref == "d" * 40


def test_supersession_outranks_a_scope_that_no_longer_exists():
    """A replaced task should read as SUPERSEDED, not as a stale scope."""
    replacement = ChangeRef(
        kind="commit",
        ref="e" * 40,
        subject="replaces %s" % TASK_ID,
        relation=ChangeRelation.REPLACES_SCOPE,
    )
    decision = decide_triage(
        _evidence(
            present_paths=(),
            missing_paths=("src/mac/widget.py",),
            candidates=(replacement,),
            markers_present=(),
            markers_absent=("def build_widget(",),
        )
    )
    assert decision.outcome == TriageOutcome.SUPERSEDED


def test_scope_stale_when_declared_paths_are_gone_from_head():
    decision = decide_triage(
        _evidence(
            present_paths=(),
            missing_paths=("src/mac/widget.py",),
            markers_present=(),
            markers_absent=("def build_widget(",),
        )
    )
    assert decision.outcome == TriageOutcome.SCOPE_STALE
    assert decision.reason == TriageReason.SCOPE_PATHS_MISSING
    assert decision.action == TriageAction.UPDATE_SCOPE
    assert "src/mac/widget.py" in decision.summary


def test_still_needed_when_head_carries_nothing():
    decision = decide_triage(
        _evidence(markers_present=(), markers_absent=("def build_widget(",))
    )
    assert decision.outcome == TriageOutcome.STILL_NEEDED
    assert decision.reason == TriageReason.NO_LANDED_CHANGE_FOUND
    assert decision.action == TriageAction.PROCEED


def test_no_head_is_cannot_tell_and_proceeds():
    decision = decide_triage(_evidence(head_sha=""))
    assert decision.outcome == TriageOutcome.CANNOT_TELL
    assert decision.reason == TriageReason.NO_HEAD_EVIDENCE
    assert decision.proceeds is True


def test_a_task_with_nothing_checkable_is_cannot_tell():
    decision = decide_triage(
        _evidence(
            scope_paths=(),
            present_paths=(),
            acceptance_markers=(),
            markers_present=(),
        )
    )
    assert decision.outcome == TriageOutcome.CANNOT_TELL
    assert decision.reason == TriageReason.NO_CHECKABLE_SCOPE


def test_a_task_with_nothing_checkable_but_mentions_names_the_mention():
    decision = decide_triage(
        _evidence(
            scope_paths=(),
            present_paths=(),
            acceptance_markers=(),
            markers_present=(),
            candidates=(_mention(),),
        )
    )
    assert decision.outcome == TriageOutcome.CANNOT_TELL
    assert decision.reason == TriageReason.MENTION_IS_NOT_A_CITATION


# --- bounded, and measured -------------------------------------------------


def test_an_exhausted_budget_is_cannot_tell_not_a_guess():
    decision = decide_triage(
        _evidence(
            cost=TriageCost(budget_exhausted=True, exhausted_by="git_calls"),
            candidates=(_implementing(),),
        )
    )
    assert decision.outcome == TriageOutcome.CANNOT_TELL
    assert decision.reason == TriageReason.BUDGET_EXHAUSTED
    assert decision.proceeds is True
    assert "git_calls" in decision.summary


def test_the_cost_of_the_read_is_measured_and_recorded():
    decision = decide_triage(
        _evidence(cost=TriageCost(git_calls=4, commits_scanned=11, elapsed_ms=3.5))
    )
    cost = ledger_payload(decision)["cost"]
    assert cost["git_calls"] == 4
    assert cost["commits_scanned"] == 11
    assert cost["elapsed_ms"] > 0


def test_the_git_call_budget_stops_the_read():
    calls = {"n": 0}

    def runner(args):
        calls["n"] += 1
        return subprocess.CompletedProcess(args, 0, "", "")

    task = _task(
        metadata={
            "triage": {
                "scope_paths": ["src/mac/widget.py"],
                "acceptance_markers": ["a", "b", "c", "d", "e", "f"],
            }
        }
    )
    evidence = collect_triage_evidence(
        task,
        head_sha="f" * 40,
        runner=runner,
        budget=TriageBudget(max_git_calls=3),
    )
    assert calls["n"] <= 3
    assert evidence.cost.budget_exhausted is True
    assert evidence.cost.exhausted_by == "git_calls"
    assert decide_triage(evidence).outcome == TriageOutcome.CANNOT_TELL


def test_the_path_probe_budget_stops_the_read():
    task = _task(metadata={"triage": {"scope_paths": ["a/b.py", "c/d.py", "e/f.py"]}})
    evidence = collect_triage_evidence(
        task,
        head_sha="f" * 40,
        runner=lambda args: subprocess.CompletedProcess(args, 0, "", ""),
        budget=TriageBudget(max_path_probes=2),
    )
    assert evidence.cost.exhausted_by == "path_probes"
    assert decide_triage(evidence).reason == TriageReason.BUDGET_EXHAUSTED


# --- reading the task ------------------------------------------------------


def test_scope_paths_prefer_the_explicit_declaration():
    task = _task(metadata={"triage": {"scope_paths": ["src/mac/other.py"]}})
    assert scope_paths_from_task(task) == ("src/mac/other.py",)


def test_scope_paths_fall_back_to_backticked_paths_in_the_description():
    assert scope_paths_from_task(_task()) == ("src/mac/widget.py",)


def test_prose_without_paths_yields_no_scope():
    task = _task(description="Make the fleet feel snappier and/or better.")
    assert scope_paths_from_task(task) == ()


def test_markers_are_never_guessed_from_prose():
    """A guessed marker that happens to match is how a coincidence closes a task."""
    assert acceptance_markers_from_task(_task()) == ()
    task = _task(metadata={"triage": {"acceptance_markers": ["def build_widget("]}})
    assert acceptance_markers_from_task(task) == ("def build_widget(",)


# --- the scope-stale correction --------------------------------------------


def test_plan_scope_update_keeps_only_the_paths_that_exist_at_head():
    decision = decide_triage(
        _evidence(
            scope_paths=("src/mac/widget.py", "src/mac/gone.py"),
            present_paths=("src/mac/widget.py",),
            missing_paths=("src/mac/gone.py",),
            markers_present=(),
            markers_absent=("def build_widget(",),
        )
    )
    assert decision.outcome == TriageOutcome.SCOPE_STALE
    update = plan_scope_update(decision, _task())
    hints = update["metadata"]["triage"]
    assert hints["scope_paths"] == ["src/mac/widget.py"]
    assert hints["removed_scope_paths"] == ["src/mac/gone.py"]
    assert hints["corrected_against_head"] == "a" * 40
    # The next attempt re-derives its scope from the corrected task, so the
    # correction has to be where scope_paths_from_task reads.
    corrected_task = _task(metadata=update["metadata"])
    assert scope_paths_from_task(corrected_task) == ("src/mac/widget.py",)
    assert update["metadata"]["triage_verdict"]["outcome"] == TriageOutcome.SCOPE_STALE


def test_plan_scope_update_refuses_any_other_verdict():
    decision = decide_triage(_evidence(candidates=(_implementing(),)))
    with pytest.raises(TriageError):
        plan_scope_update(decision, _task())


# --- the ledger record -----------------------------------------------------


def test_the_ledger_record_carries_the_citation_and_the_reason():
    decision = decide_triage(_evidence(candidates=(_implementing(), _mention())))
    payload = ledger_payload(decision)
    assert payload["outcome"] == TriageOutcome.ALREADY_LANDED
    assert payload["reason"] == TriageReason.LANDED_CITED_CHANGE
    assert payload["citation"]["ref"] == "b" * 40
    assert payload["citation"]["relation"] == ChangeRelation.IMPLEMENTS_SCOPE
    # The mention is recorded too -- an auditor needs to see what was rejected.
    assert payload["mention_only_changes"] == ["#489"]


def test_cannot_tell_is_visible_in_the_ledger_record():
    decision = decide_triage(_evidence(head_sha=""))
    payload = ledger_payload(decision)
    assert payload["outcome"] == TriageOutcome.CANNOT_TELL
    assert payload["action"] == TriageAction.PROCEED
    assert payload["citation"] is None


def test_unknown_outcomes_and_reasons_are_refused():
    with pytest.raises(TriageError):
        TriageDecision(
            outcome="probably_fine",
            reason=TriageReason.NO_LANDED_CHANGE_FOUND,
            evidence=_evidence(),
        )
    with pytest.raises(TriageError):
        TriageDecision(
            outcome=TriageOutcome.STILL_NEEDED,
            reason="felt_like_it",
            evidence=_evidence(),
        )
    with pytest.raises(TriageError):
        ChangeRef(kind="commit", ref="x" * 40, relation="sort_of_related")


# --- against a real repository ---------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "triage@example.invalid")
    _git(root, "config", "user.name", "Triage Test")
    (root / "docs").mkdir()
    (root / "docs" / "adr.md").write_text(
        "An ADR that quotes %s as motivating evidence.\n" % TASK_ID,
        encoding="utf-8",
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "ADR citing %s as evidence" % TASK_ID)
    return root


def test_a_commit_that_only_mentions_the_task_is_read_as_a_mention(repo: Path):
    """End to end: the live false positive, against a real branch head."""
    task = _task(
        metadata={
            "triage": {
                "scope_paths": ["src/mac/widget.py"],
                "acceptance_markers": ["def build_widget("],
            }
        }
    )
    evidence = collect_triage_evidence(task, worktree=repo, branch="main")
    assert evidence.head_sha
    assert [c.relation for c in evidence.candidates] == [
        ChangeRelation.MENTIONS_TASK_ID
    ]
    decision = decide_triage(evidence)
    assert decision.closes is False
    # The scope path does not exist at head, so the honest verdict is that the
    # task's scope no longer matches the tree -- not that it is done.
    assert decision.outcome == TriageOutcome.SCOPE_STALE


def test_landed_work_is_read_as_landed_and_cites_a_real_commit(repo: Path):
    src = repo / "src" / "mac"
    src.mkdir(parents=True)
    (src / "widget.py").write_text("def build_widget():\n    return 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add the widget")
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    task = _task(
        metadata={
            "triage": {
                "scope_paths": ["src/mac/widget.py"],
                "acceptance_markers": ["def build_widget("],
            }
        }
    )
    decision = decide_triage(
        collect_triage_evidence(task, worktree=repo, branch="main")
    )
    assert decision.outcome == TriageOutcome.ALREADY_LANDED
    citation = validate_close(decision)
    assert citation.ref == head
    assert citation.subject == "add the widget"


def test_a_supersedes_commit_closes_as_superseded(repo: Path):
    (repo / "docs" / "adr.md").write_text("replaced\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "widgets rewritten\n\nSupersedes %s." % TASK_ID)
    task = _task(metadata={"triage": {"scope_paths": ["docs/adr.md"]}})
    decision = decide_triage(
        collect_triage_evidence(task, worktree=repo, branch="main")
    )
    assert decision.outcome == TriageOutcome.SUPERSEDED
    assert validate_close(decision).relation == ChangeRelation.REPLACES_SCOPE


def test_a_missing_checkout_is_cannot_tell(tmp_path: Path):
    evidence = collect_triage_evidence(_task(), worktree=tmp_path / "nope")
    assert evidence.head_sha == ""
    assert decide_triage(evidence).outcome == TriageOutcome.CANNOT_TELL


def test_the_real_read_stays_inside_its_budget(repo: Path):
    task = _task(
        metadata={
            "triage": {
                "scope_paths": ["docs/adr.md"],
                "acceptance_markers": ["motivating evidence"],
            }
        }
    )
    budget = TriageBudget()
    evidence = collect_triage_evidence(task, worktree=repo, budget=budget)
    assert evidence.cost.budget_exhausted is False
    assert evidence.cost.git_calls <= budget.max_git_calls
    assert evidence.cost.path_probes <= budget.max_path_probes
