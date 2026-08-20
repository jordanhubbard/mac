"""Process selection for termination, keyed on recorded ownership only.

horde-claw-fleet ADR-0121 finding 8:

    Manual cleanup attempted to kill a stale task by grepping process command
    lines for an old task id. That id also appeared in ACTIVE replacement-task
    prompts as contextual text, so active tasks were killed too. Cleanup must
    use recorded pid/process-group/task ownership metadata rather than free-text
    prompt matching.

MAC's abort works by lease revocation and worker polling, which is safe. This
module exists so that anything which later *does* reach for a process tree
cannot reintroduce the failure. The defence is structural rather than a
convention: :class:`ProcessOwnership` has no field that can hold a command
line, prompt, or argv, and :func:`select_owned_processes` compares the recorded
``task_id`` for equality and nothing else. There is no code path from text to a
signal, so a task id quoted inside another task's prompt is not merely filtered
out -- it is never read.

The ADR's exact failure is the regression test in
``tests/test_process_ownership_kill_selection.py``.
"""

from __future__ import annotations

import os
import re
import signal as signal_module
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, List, Optional


JsonDict = Dict[str, Any]

PROCESS_OWNERSHIP_SCHEMA = "mac.process_ownership.v1"

_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{32}$")
_LEASE_ID_RE = re.compile(r"^lease_[0-9a-f]{1,32}$")

#: Keys that carry free text a caller might be tempted to match against.
#: Passing any of them to :func:`process_ownership_record` is an error rather
#: than a silently dropped field, so the mistake is caught where it is made.
FORBIDDEN_SELECTOR_KEYS = frozenset(
    {
        "cmdline",
        "command",
        "command_line",
        "args",
        "argv",
        "prompt",
        "description",
        "title",
        "text",
    }
)


class ProcessOwnershipError(ValueError):
    """Raised when an ownership record is unusable as a termination selector."""


@dataclass(frozen=True)
class ProcessOwnership:
    """Who a running process belongs to, recorded when it is spawned.

    Every field is an identifier the spawner *knew*, never something scraped
    back out of the process table. There is deliberately no command-line field:
    the type cannot represent the input that caused the ADR-0121 incident.
    """

    task_id: str
    lease_id: str
    agent_id: str
    pid: int
    pgid: int

    def to_dict(self) -> JsonDict:
        record = asdict(self)
        record["schema"] = PROCESS_OWNERSHIP_SCHEMA
        return record


def process_ownership_record(
    *,
    task_id: str,
    lease_id: str,
    agent_id: str,
    pid: int,
    pgid: Optional[int] = None,
    **rejected: Any,
) -> ProcessOwnership:
    """Build a validated ownership record for a process being spawned.

    ``pgid`` defaults to ``pid``, matching a process started in its own process
    group (``start_new_session``/``setsid``), which is how MAC's executors run.
    """

    if rejected:
        offending = sorted(set(rejected) & FORBIDDEN_SELECTOR_KEYS) or sorted(rejected)
        raise ProcessOwnershipError(
            "process ownership records carry identifiers only; refusing "
            "free-text field(s) %s -- selecting processes by matching text is "
            "the ADR-0121 finding 8 failure" % ", ".join(offending)
        )
    task_id = str(task_id or "").strip()
    lease_id = str(lease_id or "").strip()
    agent_id = str(agent_id or "").strip()
    if not _TASK_ID_RE.fullmatch(task_id):
        raise ProcessOwnershipError(
            "process ownership requires a task_<32 hex> task_id, got %r" % task_id
        )
    if not _LEASE_ID_RE.fullmatch(lease_id):
        raise ProcessOwnershipError(
            "process ownership requires a lease_<hex> lease_id, got %r" % lease_id
        )
    if not agent_id:
        raise ProcessOwnershipError("process ownership requires an agent_id")
    try:
        pid_value = int(pid)
        pgid_value = int(pid if pgid is None else pgid)
    except (TypeError, ValueError) as exc:
        raise ProcessOwnershipError("pid and pgid must be integers") from exc
    if pid_value <= 1 or pgid_value <= 1:
        # pid 0 means "every process in our group" and pid 1 is init; both are
        # catastrophic signal targets and neither is ever a MAC executor.
        raise ProcessOwnershipError(
            "refusing ownership record with pid=%d pgid=%d; 0 and 1 are never "
            "task-owned processes" % (pid_value, pgid_value)
        )
    return ProcessOwnership(
        task_id=task_id,
        lease_id=lease_id,
        agent_id=agent_id,
        pid=pid_value,
        pgid=pgid_value,
    )


def _coerce(record: Any) -> Optional[ProcessOwnership]:
    if isinstance(record, ProcessOwnership):
        return record
    if not isinstance(record, Mapping):
        return None
    try:
        return process_ownership_record(
            task_id=record.get("task_id"),
            lease_id=record.get("lease_id"),
            agent_id=record.get("agent_id"),
            pid=record.get("pid"),
            pgid=record.get("pgid"),
        )
    except ProcessOwnershipError:
        return None


def select_owned_processes(
    records: Iterable[Any],
    *,
    task_id: str,
    lease_id: str = "",
) -> List[ProcessOwnership]:
    """Return the records owned by *task_id* -- exact identifier match only.

    ``lease_id``, when given, narrows further to a single attempt, so a stale
    attempt can be reaped without touching the live one that replaced it.

    Matching is ``==`` on the recorded ``task_id``. It is not a substring
    search, not a regex, and there is no text for it to search: whether some
    other task's prompt quotes this id is invisible here.
    """

    wanted_task = str(task_id or "").strip()
    if not _TASK_ID_RE.fullmatch(wanted_task):
        raise ProcessOwnershipError(
            "termination must name a task_<32 hex> id, got %r" % task_id
        )
    wanted_lease = str(lease_id or "").strip()
    if wanted_lease and not _LEASE_ID_RE.fullmatch(wanted_lease):
        raise ProcessOwnershipError(
            "termination lease filter must be a lease_<hex> id, got %r" % lease_id
        )
    selected: List[ProcessOwnership] = []
    for record in records or ():
        owned = _coerce(record)
        if owned is None:
            continue
        if owned.task_id != wanted_task:
            continue
        if wanted_lease and owned.lease_id != wanted_lease:
            continue
        selected.append(owned)
    return selected


def terminate_owned_processes(
    records: Iterable[Any],
    *,
    task_id: str,
    lease_id: str = "",
    sig: int = signal_module.SIGTERM,
    killpg: Callable[[int, int], None] = os.killpg,
) -> JsonDict:
    """Signal every process recorded as owned by *task_id*.

    Signals the recorded process *group* so an executor's own children die with
    it, and reports each outcome rather than raising: a process that already
    exited is the expected case during cleanup, not a failure.
    """

    selected = select_owned_processes(records, task_id=task_id, lease_id=lease_id)
    signalled: List[JsonDict] = []
    for owned in selected:
        outcome: JsonDict = {
            "task_id": owned.task_id,
            "lease_id": owned.lease_id,
            "pgid": owned.pgid,
            "pid": owned.pid,
        }
        try:
            killpg(owned.pgid, sig)
        except ProcessLookupError:
            outcome["result"] = "already_exited"
        except PermissionError as exc:
            outcome["result"] = "permission_denied"
            outcome["error"] = str(exc)[:200]
        except OSError as exc:
            outcome["result"] = "error"
            outcome["error"] = str(exc)[:200]
        else:
            outcome["result"] = "signalled"
        signalled.append(outcome)
    return {
        "schema": PROCESS_OWNERSHIP_SCHEMA,
        "task_id": str(task_id or "").strip(),
        "lease_id": str(lease_id or "").strip(),
        "signal": int(sig),
        "selected": len(selected),
        "processes": signalled,
    }
