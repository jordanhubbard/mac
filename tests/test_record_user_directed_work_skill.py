"""`record-user-directed-work` tells an agent how to turn conversation into
durable ledger state. Its advice is mostly about ORDER -- record before
implementing, deploy before releasing -- and order is exactly the kind of claim
that goes quietly wrong when the primitives underneath it change.

Two changed since the file was last read. `mac task update` stopped meaning
"edit in place" and became an atomic stop/edit/restart (ADR 0020), which adds
consequences the skill has to state. And the retry classifier now reads a
failure's MESSAGE rather than its reason code, so the skill's justification for
the deploy-before-release rule -- "`repository_test_failed` is classified
non-retryable" -- was describing a mechanism that does not exist.

The classifier is therefore not asserted by string matching; it is called.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.skill_claims import assert_commands_exist, assert_flags_exist
from tests.test_mac_cli_skill import _command_tree, _options_for

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "record-user-directed-work" / "SKILL.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert SKILL.is_file(), "the record-user-directed-work skill is missing"
    return SKILL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tree():
    return _command_tree()


# ---------------------------------------------------------------------------
# The commands it tells the reader to run
# ---------------------------------------------------------------------------


def test_every_command_the_skill_names_exists(text, tree):
    assert_commands_exist(text, tree)


def test_every_flag_the_skill_names_exists(text):
    assert_flags_exist(text, _options_for)


def test_the_memory_store_really_did_move_under_admin(tree):
    """The skill says "note the `admin`". If the top-level spelling ever comes
    back, that warning becomes a lie -- and if `admin memory` moves again, the
    command it hands the reader stops working."""

    assert ("admin", "memory", "remember") in tree
    assert ("memory",) not in tree
    assert ("memory", "remember") not in tree


def test_the_ledger_is_the_record_and_the_mirror_stays_out_of_git():
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".tickets/" in [line.strip() for line in gitignore], (
        "the skill says .tickets/ is a gitignored local mirror"
    )


# ---------------------------------------------------------------------------
# ADR 0020: editing a task that is already in flight
# ---------------------------------------------------------------------------


def test_the_edit_cycle_verbs_exist_and_are_shaped_as_the_skill_says(tree):
    assert ("task", "update") in tree
    assert ("task", "stop") in tree
    assert ("task", "start") in tree
    assert "--reason" in _options_for(("task", "stop")), (
        "the skill tells the reader to give a reason when stopping"
    )
    # `start` is not a second place to edit the task; that is what `update` is
    # for, and the skill says a restart re-enters from the top.
    assert "--description" not in _options_for(("task", "start"))


def test_stopped_is_a_real_state_and_is_not_terminal():
    from mac.models import TERMINAL_TASK_STATES, TaskState

    assert TaskState.STOPPED.value == "stopped"
    assert "stopped" not in TERMINAL_TASK_STATES, (
        "the skill says a STOPPED task is live work, not an ending"
    )


def test_update_still_performs_the_stop_edit_restart_cycle_itself():
    """The skill's advice -- that `mac task update` is safe on a RUNNING task
    because the hub does the cycle for you -- is only safe advice while the
    cycle lives below the API. If it is ever pushed back onto callers, an agent
    following this skill would edit a task out from under its own executor."""

    import inspect

    from mac.services import ControlPlane

    assert hasattr(ControlPlane, "stop_task")
    assert hasattr(ControlPlane, "start_stopped_task")
    doc = inspect.getdoc(ControlPlane.update_task) or ""
    assert "ADR 0020" in doc
    assert "restart" in doc.lower() and "lease" in doc.lower()


def test_update_stop_and_start_are_separate_permissions():
    """ADR 0019. The skill tells an agent it cannot stop its own task or
    rewrite its own criteria. That holds only while these are distinct
    permissions and none implies another."""

    from mac.acl import PERMISSIONS, Permission

    for permission in (Permission.UPDATE, Permission.STOP, Permission.START):
        assert permission in PERMISSIONS
    assert len({Permission.UPDATE, Permission.STOP, Permission.START, Permission.WRITE,
                Permission.CONTROL}) == 5, "stop/start/update folded back into write/control"


# ---------------------------------------------------------------------------
# Dispatch holds
# ---------------------------------------------------------------------------


def test_the_no_dispatch_hold_and_its_release_verb_both_exist(tree):
    assert "--no-dispatch" in _options_for(("task", "create"))
    assert ("task", "release") in tree

    cli = (ROOT / "src" / "mac" / "cli.py").read_text(encoding="utf-8")
    assert 'metadata["no_dispatch"] = True' in cli, (
        "the skill says the hold is metadata; if it moved, `release` advice may be stale"
    )

    from mac.services import ControlPlane

    assert hasattr(ControlPlane, "release_task")


def test_why_unclaimed_still_names_every_gate_the_skill_lists(text):
    from mac.cli import _WHY_UNCLAIMED_HINTS

    for gate in (
        "task_dispatch_held",
        "task_project_paused",
        "task_dependencies_unmet",
        "agent_held",
        "agent_project_not_allowed",
    ):
        assert gate in _WHY_UNCLAIMED_HINTS, "why-unclaimed no longer reports %s" % gate
        assert gate in text, "the skill should list the %s gate" % gate
    assert "mac task release" in _WHY_UNCLAIMED_HINTS["task_dispatch_held"]


# ---------------------------------------------------------------------------
# The ordering rule's justification: the retry classifier, called not quoted
# ---------------------------------------------------------------------------


def test_work_failures_stop_and_are_not_retried():
    """The reason the skill says to deploy before releasing the backlog."""

    from mac.services import _blocked_attempt_retry_kind

    for detail in (
        {"reason": "repository_test_failed", "detail": "2 tests failed"},
        {"reason": "verification_contract_failed"},
        {"reason": "repository_verification_failed", "detail": "repo evidence requires a pushed ref"},
        {"detail": "required changed files were not modified"},
    ):
        assert _blocked_attempt_retry_kind(detail) == "non_retryable", detail


def test_an_unrecognised_failure_is_transient_not_permanent():
    """The correction. Classification is by MESSAGE, so a reason code alone --
    including `repository_test_failed` itself -- does not make a failure
    terminal. A skill that says otherwise teaches the reader to expect a
    tranche of FAILED tasks that will in fact be retried, and to misread the
    ones that are not."""

    from mac.services import _blocked_attempt_retry_kind

    assert (
        _blocked_attempt_retry_kind(
            {"reason": "repository_test_failed", "detail": "verifier exited with status 1"}
        )
        == "transient"
    )
    assert (
        _blocked_attempt_retry_kind(
            {"reason": "openshell repository verifier did not start"}
        )
        == "infrastructure_transient"
    )


def test_the_markers_the_skill_quotes_are_the_markers_that_are_matched(text):
    from mac.services import _DETERMINISTIC_FAILURE_MARKERS

    for marker in (
        "tests failed",
        "verification_contract_failed",
        "repo evidence requires",
        "required changed files",
    ):
        assert marker in _DETERMINISTIC_FAILURE_MARKERS, (
            "the skill quotes %r as a terminal marker" % marker
        )
        assert marker in text, "the skill should quote the %r marker" % marker


def test_reopen_is_still_the_recovery_for_an_exhausted_budget(tree):
    import inspect

    from mac.services import ControlPlane

    assert ("task", "reopen") in tree
    doc = inspect.getdoc(ControlPlane.reopen_task) or ""
    assert "attempt_count" in doc and "resets" in doc, (
        "the skill promises reopen resets the attempt budget"
    )
