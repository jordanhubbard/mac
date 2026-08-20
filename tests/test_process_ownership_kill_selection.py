"""ADR-0121 finding 8: cleanup keys on recorded ownership, never on text.

    Manual cleanup attempted to kill a stale task by grepping process command
    lines for an old task id. That id also appeared in ACTIVE replacement-task
    prompts as contextual text, so active tasks were killed too.

The required regression is the last test in this file: a task id appearing in
another task's prompt must not select that task's process for termination.
"""

from __future__ import annotations

import signal

import pytest

from mac.process_ownership import (
    FORBIDDEN_SELECTOR_KEYS,
    ProcessOwnership,
    ProcessOwnershipError,
    process_ownership_record,
    select_owned_processes,
    terminate_owned_processes,
)


STALE_TASK = "task_" + "a" * 32
ACTIVE_TASK = "task_" + "b" * 32
LEASE_A = "lease_" + "c" * 16
LEASE_B = "lease_" + "d" * 16


def _record(task_id, lease_id, pid):
    return process_ownership_record(
        task_id=task_id,
        lease_id=lease_id,
        agent_id="agent_worker",
        pid=pid,
    )


def test_ownership_record_defaults_the_process_group_to_the_pid():
    owned = _record(STALE_TASK, LEASE_A, 4242)
    assert owned.pgid == 4242
    assert owned.to_dict()["schema"] == "mac.process_ownership.v1"


def test_ownership_record_refuses_pid_zero_and_init():
    for pid in (0, 1, -1):
        with pytest.raises(ProcessOwnershipError, match="never task-owned"):
            _record(STALE_TASK, LEASE_A, pid)


def test_ownership_record_refuses_free_text_fields_outright():
    # Not silently dropped: the mistake is refused where it is made.
    with pytest.raises(ProcessOwnershipError, match="free-text"):
        process_ownership_record(
            task_id=STALE_TASK,
            lease_id=LEASE_A,
            agent_id="agent_worker",
            pid=4242,
            cmdline="python worker.py --task %s" % STALE_TASK,
        )


def test_the_record_type_cannot_hold_a_command_line_at_all():
    # Structural, not conventional: there is no field for the input that
    # caused the incident.
    assert not FORBIDDEN_SELECTOR_KEYS & set(ProcessOwnership.__dataclass_fields__)
    with pytest.raises(TypeError):
        ProcessOwnership(
            task_id=STALE_TASK,
            lease_id=LEASE_A,
            agent_id="agent_worker",
            pid=1,
            pgid=1,
            cmdline="anything",
        )


def test_selection_requires_a_real_task_id():
    with pytest.raises(ProcessOwnershipError, match="task_<32 hex>"):
        select_owned_processes([], task_id="7f84dddd")


def test_selection_can_narrow_to_a_single_stale_attempt():
    live = _record(STALE_TASK, LEASE_B, 200)
    stale = _record(STALE_TASK, LEASE_A, 100)
    selected = select_owned_processes([stale, live], task_id=STALE_TASK, lease_id=LEASE_A)
    assert [owned.pid for owned in selected] == [100]


def test_termination_signals_the_recorded_process_group_and_reports_outcomes():
    signalled = []

    def fake_killpg(pgid, sig):
        signalled.append((pgid, sig))

    result = terminate_owned_processes(
        [_record(STALE_TASK, LEASE_A, 100), _record(ACTIVE_TASK, LEASE_B, 200)],
        task_id=STALE_TASK,
        killpg=fake_killpg,
    )
    assert signalled == [(100, signal.SIGTERM)]
    assert result["selected"] == 1
    assert result["processes"][0]["result"] == "signalled"


def test_an_already_exited_process_is_not_a_cleanup_failure():
    def fake_killpg(pgid, sig):
        raise ProcessLookupError(pgid)

    result = terminate_owned_processes(
        [_record(STALE_TASK, LEASE_A, 100)],
        task_id=STALE_TASK,
        killpg=fake_killpg,
    )
    assert result["processes"][0]["result"] == "already_exited"


def test_a_task_id_quoted_in_another_tasks_prompt_cannot_select_it():
    """The ADR-0121 finding 8 regression, stated exactly.

    The active task's prompt quotes the stale task's id as context -- which is
    what a replacement task's prompt legitimately does, since it has to explain
    what it replaces. Terminating the stale task must signal only the stale
    task's own process.
    """

    active_prompt = (
        "Replacement for %s. The prior attempt at %s produced a divergent "
        "implementation; do not repeat it." % (STALE_TASK, STALE_TASK)
    )
    assert STALE_TASK in active_prompt  # the trap the old cleanup fell into

    stale_process = _record(STALE_TASK, LEASE_A, 100)
    active_process = _record(ACTIVE_TASK, LEASE_B, 200)

    signalled = []
    result = terminate_owned_processes(
        [stale_process, active_process],
        task_id=STALE_TASK,
        killpg=lambda pgid, sig: signalled.append(pgid),
    )

    assert signalled == [100]
    assert result["selected"] == 1
    assert all(
        entry["task_id"] == STALE_TASK for entry in result["processes"]
    )
    # And the active task's process is not merely unsignalled -- it is not
    # selectable, because nothing in the selector ever reads prompt text.
    assert select_owned_processes(
        [stale_process, active_process], task_id=ACTIVE_TASK
    ) == [active_process]
