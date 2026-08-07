"""A blocked task must say what is blocking it, and what to do next.

Reported from real use on 2026-08-07. `mac task show task_c58a745d` rendered:

    task_c58a745d blocked   nanolang  Make sdl_audio_wav.nano default to ...
      dependencies: 1
      ...
    Activity:
      - diagnosis / dependency-reconciliation
          Problem: Task blocked: dependencies_incomplete
          Remediation: Inspect the task evidence + history (`mac task show`)
                       and the agent workspace logs for the root cause.

Two defects in one screen. The blocking dependency was rendered as the bare
count ``dependencies: 1`` while the record held both its id and its state, and
the remediation told the reader to run the command they were already reading --
advice that returns them to the same page. The operator's words: "the logic
seems circular".

The record had the whole story:

    metadata.dependency_resolution.unsatisfied = {
        "task_d9eef627...": {"state": "failed", ...}
    }

The task was waiting on "Add a license-clean, Mix_LoadWAV-playable WAV asset",
which had failed 3/3. Nothing in the human-facing output said so.

A blocked task is never about itself -- the work is in its dependency -- so the
output now names the blockers with their states and points at the next command
rather than back at itself.
"""

from __future__ import annotations

import pytest

from mac.cli import _blocking_dependency_lines


def _task(unsatisfied, **extra):
    metadata = {
        "dependency_resolution": {
            "schema": "mac.dependency_resolution.v1",
            "status": "unsatisfied",
            "policy": "supervise",
            "unsatisfied": unsatisfied,
        }
    }
    metadata.update(extra)
    return {"id": "task_child", "state": "blocked", "metadata": metadata}


def test_the_blocking_dependency_is_named_with_its_state():
    """The fact the reader needed and could not see."""
    lines = _blocking_dependency_lines(
        _task({"task_d9eef627fa4f4600a41656cb1254bb76": {"state": "failed"}})
    )
    text = "\n".join(lines)

    assert "blocked by:" in text
    assert "task_d9eef627" in text
    assert "failed" in text


def test_it_points_at_the_next_step_not_back_at_itself():
    """The circularity the operator hit.

    The old remediation said to run `mac task show`, which is the page that
    printed it. Advice that returns the reader to where they already are is
    worse than none: it reads like an answer.
    """
    text = "\n".join(
        _blocking_dependency_lines({"metadata": {"dependency_resolution": {
            "unsatisfied": {"task_dep": {"state": "failed"}}}}})
    )

    assert "join:" in text, "the advice must be grounded in this task's join"
    assert "mac task reopen" in text, "no legal route forward named"
    # NOT `mac task cancel <blocker>`: cancel is not a legal transition from
    # failed, and under all_success a cancelled dependency would leave this
    # task blocked anyway. An earlier version of this advice said exactly
    # that and sent the operator into a 400.
    assert "cancel this task rather than the blocker" in text


def test_several_blockers_are_all_named():
    lines = _blocking_dependency_lines(
        _task(
            {
                "task_aaaaaaaa1111": {"state": "failed"},
                "task_bbbbbbbb2222": {"state": "blocked"},
            }
        )
    )
    text = "\n".join(lines)

    assert "task_aaaaaaaa" in text and "failed" in text
    assert "task_bbbbbbbb" in text and "blocked" in text


def test_a_title_is_shown_when_the_payload_carries_one():
    """An id alone still costs the reader another command."""
    lines = _blocking_dependency_lines(
        _task({"task_dep1": {"state": "failed"}}),
        {"dependency_tasks": [{"id": "task_dep1", "title": "Add a WAV asset"}]},
    )

    assert "Add a WAV asset" in "\n".join(lines)


def test_a_dependency_without_a_recorded_state_still_appears():
    """Fail toward saying something. A blocker with no state is still a blocker."""
    lines = _blocking_dependency_lines(_task({"task_dep1": {}}))

    assert "task_dep1" in "\n".join(lines)
    assert "unknown state" in "\n".join(lines)


@pytest.mark.parametrize(
    "task",
    [
        {},
        {"metadata": None},
        {"metadata": {}},
        {"metadata": {"dependency_resolution": None}},
        {"metadata": {"dependency_resolution": {}}},
        {"metadata": {"dependency_resolution": {"unsatisfied": {}}}},
        {"metadata": {"dependency_resolution": {"unsatisfied": "junk"}}},
    ],
)
def test_a_task_with_nothing_blocking_it_adds_no_noise(task):
    """This section must appear only when there is something to say."""
    assert _blocking_dependency_lines(task) == []


def test_the_dependency_remediation_is_no_longer_circular():
    """The generated advice, not just the rendering.

    A NEW diagnosis for a dependency block must name the dependency as the
    place to work, rather than pointing at `mac task show`.
    """
    from mac.services import _failure_diagnosis

    note = _failure_diagnosis("blocked", {"reason": "dependencies_incomplete"})

    assert note, "a dependency block produced no diagnosis at all"
    assert "This task is fine; its input is not" in note, (
        "the diagnosis does not tell the reader the work is elsewhere"
    )
    assert "blocking dependencies" in note
    assert "mac task reopen" in note, "no legal route forward for a failed blocker"
    assert "not straight to" in note, (
        "the advice must say cancel is illegal from failed; the first version "
        "of it told the operator to cancel a failed task and produced a 400"
    )


def test_the_generic_remediation_is_still_used_for_other_failures():
    """Only the circular case changed; unrelated failures keep their advice."""
    from mac.services import _failure_diagnosis

    note = _failure_diagnosis("failed", {"reason": "executor exploded"})

    assert note and "executor exploded" in note


# --------------------------------------------------------------------------
# A hub refusal is not a crash
# --------------------------------------------------------------------------


def test_a_hub_refusal_renders_as_one_line_not_a_traceback():
    """`mac task cancel` on a failed task produced ~30 lines of urllib stack.

    HubClientError is a RuntimeError, not a MACError, so it escaped the CLI's
    error handling entirely. A stack trace is what you print when you do not
    know what happened; the hub had said exactly what happened.
    """
    from mac.cli import _hub_error_message
    from mac.http_client import HubClientError

    exc = HubClientError(
        'HTTP 400 Bad Request: {"detail":"cannot transition task from failed to cancelled"}'
    )
    message = _hub_error_message(exc)

    assert message.startswith("cannot transition task from failed to cancelled")
    assert "Traceback" not in message
    assert "urllib" not in message


def test_the_refusal_names_what_is_actually_possible():
    """"cannot transition from failed to cancelled" is true and still leaves
    the reader guessing. The legal moves are in TASK_TRANSITIONS."""
    from mac.cli import _hub_error_message
    from mac.http_client import HubClientError

    message = _hub_error_message(
        HubClientError(
            'HTTP 400 Bad Request: {"detail":"cannot transition task from failed to cancelled"}'
        )
    )

    assert "only legal move is: open" in message
    assert "mac task reopen" in message
    assert "reopen first, then `mac task cancel`" in message


def test_a_non_hub_exception_keeps_its_traceback():
    """Genuine bugs must not be flattened into a friendly line.

    Suppressing those would trade one bad experience for a worse one.
    """
    from mac.cli import _hub_error_message

    assert _hub_error_message(ValueError("a real bug")) is None
    assert _hub_error_message(KeyError("boom")) is None


def test_a_refusal_without_a_json_body_still_renders():
    from mac.cli import _hub_error_message
    from mac.http_client import HubClientError

    message = _hub_error_message(HubClientError("Connection refused"))

    assert "Connection refused" in message


def test_a_refusal_that_is_not_a_transition_gets_no_invented_hint():
    from mac.cli import _hub_error_message
    from mac.http_client import HubClientError

    message = _hub_error_message(
        HubClientError('HTTP 403 Forbidden: {"detail":"admin scope required"}')
    )

    assert message == "admin scope required"
