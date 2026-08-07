"""Behavioural coverage for the CRUD verbs the CLI was missing.

Two of the five CRUD verbs on the first-class objects had no command at all:

``mac task update``
    ``ControlPlane.update_task`` has always existed and nothing called it from
    the command line. The nearest thing a user could find was ``mac task
    edit``, which is a different operation -- it opens $EDITOR to ANSWER a task
    parked on a human question and refuses unless the task is in NEEDS_INPUT.

``mac agent show``
    Every other first-class object could be read by id. ``mac agent config
    show`` existed and answers a richer question (identity, runtime flags,
    gateway-reported deploy config, mood); the plain read did not exist.

These run the real commands against a real control plane, so they cover the
handlers rather than the parser wiring -- tests/cli/test_cli_first_class_surface.py
covers the vocabulary and the aliasing.
"""

from __future__ import annotations

import io
import json
import sys

import pytest

from mac.cli import main
from mac.test_support import dsn_for


def _run(tmp_path, *args):
    """Run `mac --json --db <tmp> <args>` and return (rc, parsed_output)."""
    out = io.StringIO()
    old = sys.stdout
    sys.stdout = out
    try:
        rc = main(["--json", "--db", dsn_for(tmp_path), *args])
    finally:
        sys.stdout = old
    raw = out.getvalue().strip()
    return rc, json.loads(raw) if raw else None


@pytest.fixture()
def project(tmp_path):
    rc, _ = _run(tmp_path, "project", "create", "mac")
    assert rc == 0
    return "mac"


def _task(tmp_path, project, title="original title"):
    rc, task = _run(tmp_path, "task", "create", title, "--project", project)
    assert rc == 0
    return task


# --------------------------------------------------------------------------
# mac task update
# --------------------------------------------------------------------------


def test_task_update_changes_the_title(tmp_path, project):
    task = _task(tmp_path, project)

    rc, updated = _run(tmp_path, "task", "update", task["id"], "--title", "a better title")

    assert rc == 0
    assert updated["title"] == "a better title"


def test_task_update_leaves_omitted_fields_alone(tmp_path, project):
    """A partial update must not blank the fields it was not given."""
    rc, task = _run(
        tmp_path,
        "task",
        "create",
        "keep my description",
        "--project",
        project,
        "--description",
        "the original description",
        "--priority",
        "3",
    )
    assert rc == 0

    _rc, after = _run(tmp_path, "task", "update", task["id"], "--title", "renamed")

    assert after["title"] == "renamed"
    assert after["description"] == "the original description"
    assert after["priority"] == 3


def test_task_update_changes_priority_and_capabilities(tmp_path, project):
    task = _task(tmp_path, project)

    rc, updated = _run(
        tmp_path,
        "task",
        "update",
        task["id"],
        "--priority",
        "7",
        "--capabilities",
        "python,metal",
    )

    assert rc == 0
    assert updated["priority"] == 7
    assert sorted(updated["required_capabilities"]) == ["metal", "python"]


def test_task_update_with_no_fields_is_refused(tmp_path, project):
    """Silently succeeding on a no-op update would hide a typo'd flag."""
    task = _task(tmp_path, project)

    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, "task", "update", task["id"])

    assert "nothing to update" in str(excinfo.value)


def test_task_update_reads_a_description_from_a_file(tmp_path, project):
    """The repo's convention for multi-line / shell-hostile content."""
    task = _task(tmp_path, project)
    body = tmp_path / "body.txt"
    body.write_text("a description with `backticks` and $VARS\nand a newline\n", encoding="utf-8")

    rc, updated = _run(
        tmp_path, "task", "update", task["id"], "--description-file", str(body)
    )

    assert rc == 0
    assert "backticks" in updated["description"]
    assert "\n" in updated["description"]


def test_task_update_is_reachable_by_its_own_name(tmp_path, project):
    """Not an alias of `edit`: `edit` refuses a task that is not NEEDS_INPUT,
    and this one does not."""
    task = _task(tmp_path, project)

    rc, _ = _run(tmp_path, "task", "update", task["id"], "--title", "renamed")

    assert rc == 0


# --------------------------------------------------------------------------
# mac agent show
# --------------------------------------------------------------------------


@pytest.fixture()
def agent(tmp_path):
    rc, machine = _run(tmp_path, "machine", "register", "host-a")
    assert rc == 0
    rc, agent = _run(
        tmp_path, "agent", "register", machine["id"], "worker-a", "--capabilities", "python"
    )
    assert rc == 0
    return agent


def test_agent_show_reads_one_agent(tmp_path, agent):
    rc, shown = _run(tmp_path, "agent", "show", agent["id"])

    assert rc == 0
    assert shown["id"] == agent["id"]
    assert shown["name"] == "worker-a"


def test_agent_show_reports_an_unknown_agent(tmp_path, agent):
    """A read of something absent must fail, not return an empty record.

    The CLI maps a domain error to a non-zero exit and a stderr line rather
    than letting the exception escape, so that is what a caller sees.
    """
    rc, payload = _run(tmp_path, "agent", "show", "agent_does_not_exist")

    assert rc != 0
    assert payload is None


def test_agent_create_is_the_same_command_as_register(tmp_path):
    """The alias has to actually register an agent, not merely parse."""
    rc, machine = _run(tmp_path, "machine", "register", "host-b")
    assert rc == 0

    rc, created = _run(
        tmp_path, "agent", "create", machine["id"], "worker-b", "--capabilities", "python"
    )

    assert rc == 0
    _rc, shown = _run(tmp_path, "agent", "show", created["id"])
    assert shown["name"] == "worker-b"


# --------------------------------------------------------------------------
# mac task cancel / delete from a terminal state
# --------------------------------------------------------------------------


def _force_state(tmp_path, task_id, state):
    """Put a task into `state` in the SAME store the CLI reads.

    Deliberately not ControlPlane(ephemeral_store(...)): that creates a NEW
    schema, so the write lands somewhere the CLI never looks. Two tests here
    passed vacuously that way -- cancelling from `open` succeeds regardless,
    so they proved nothing about cancelling from a terminal state.
    """
    from mac.store import open_postgres_store

    store = open_postgres_store(dsn_for(tmp_path), initialize_schema=False)
    store.execute("UPDATE tasks SET state = ? WHERE id = ?", (state, task_id))


def _state(tmp_path, task_id):
    _rc, detail = _run(tmp_path, "task", "show", task_id)
    task = detail.get("task", detail) if isinstance(detail, dict) else detail
    return task.get("state")


def test_cancelling_a_failed_task_succeeds(tmp_path, project):
    """"Cancel" means make this stop and go away, and that intent does not
    change because the task already failed.

    The state machine only allows failed -> open, so this used to return
    HTTP 400 and hand the operator a two-step dance to express one decision.
    """
    task = _task(tmp_path, project)
    from mac.models import TaskState

    _force_state(tmp_path, task["id"], TaskState.FAILED.value)

    rc, _ = _run(tmp_path, "task", "cancel", task["id"], "--reason", "abandoned")

    assert rc == 0
    assert _state(tmp_path, task["id"]) == TaskState.CANCELLED.value


def test_the_reopen_is_recorded_as_part_of_the_cancellation(tmp_path, project):
    """The history must not read as though someone intended a retry."""
    from mac.models import TaskState
    task = _task(tmp_path, project)
    _force_state(tmp_path, task["id"], TaskState.FAILED.value)
    _run(tmp_path, "task", "cancel", task["id"], "--reason", "no licence-clean asset")

    _rc, detail = _run(tmp_path, "task", "show", task["id"])
    reasons = " ".join(
        str((h.get("detail") or {}).get("reason", "")) for h in detail.get("history") or []
    )

    assert "reopened only to cancel" in reasons, (
        "the reopen is indistinguishable from a genuine retry in the history"
    )


def test_cancelling_an_already_cancelled_task_is_not_an_error(tmp_path, project):
    """It is already where the operator wants it; saying so beats a 400."""
    from mac.models import TaskState
    task = _task(tmp_path, project)
    _force_state(tmp_path, task["id"], TaskState.CANCELLED.value)

    rc, _ = _run(tmp_path, "task", "cancel", task["id"])

    assert rc == 0
    assert _state(tmp_path, task["id"]) == TaskState.CANCELLED.value


def test_cancelling_a_completed_task_is_refused(tmp_path, project):
    """The one terminal state that must NOT be walked back.

    Completed work is a real outcome with evidence and a publication behind
    it. Quietly erasing that is not what anyone means by "cancel".
    """
    from mac.models import TaskState
    task = _task(tmp_path, project)
    _force_state(tmp_path, task["id"], TaskState.COMPLETED.value)

    with pytest.raises(SystemExit) as excinfo:
        _run(tmp_path, "task", "cancel", task["id"])

    assert "completed" in str(excinfo.value)
    assert _state(tmp_path, task["id"]) == TaskState.COMPLETED.value


def test_delete_is_the_same_command_and_gets_the_same_behaviour(tmp_path, project):
    """`task delete` aliases `cancel`, so it must inherit this too."""
    from mac.models import TaskState
    task = _task(tmp_path, project)
    _force_state(tmp_path, task["id"], TaskState.FAILED.value)

    rc, _ = _run(tmp_path, "task", "delete", task["id"], "--reason", "abandoned")

    assert rc == 0
    assert _state(tmp_path, task["id"]) == TaskState.CANCELLED.value
