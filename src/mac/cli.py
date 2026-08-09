"""Command-line interface for the MAC control plane.

Parses arguments and dispatches the ``mac`` subcommands that operate on tasks,
agents, repositories, and local ledger state, wiring terminal input to the
underlying services and store.
"""

from __future__ import annotations

import argparse
import json
import re
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from mac import mac_paths
from mac.migration import import_jsonl, migrate_acc_sqlite
from mac.task_batch import OPERATIONS as BATCH_OPERATIONS
from mac.models import (
    EVIDENCE_KIND_CHOICES,
    MACError,
    REPORT_DELIVERABLE,
    normalize_deliverable_kind,
    normalize_evidence_kind,
    parse_time,
    utcnow,
)
from mac import sandbox_bom
from mac.task_wait import WAIT_SCHEMA, TaskWait, dedupe_events, waitable_tasks
from mac.repository_hygiene import (
    CANCELLATION_DISPOSITIONS,
    REPOSITORY_REF_CLEANUP_SCHEMA,
    RepositoryHygieneError,
    RepositoryRefAudit,
    audit_repository_refs_result,
    cleanup_evidence_metadata,
    list_managed_remote_refs,
    prune_repository_refs,
    query_open_pull_requests,
)
def _json_arg(value: Optional[str], default: Any) -> Any:
    if value is None:
        return default
    return json.loads(value)


def _csv(value: Optional[str]) -> Iterable[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _review_arm_weights(value: Optional[str]) -> Optional[Dict[str, float]]:
    """Parse ``arm=weight,arm=weight`` without hiding invalid input."""
    if value is None:
        return None
    weights: Dict[str, float] = {}
    for item in _csv(value):
        if "=" not in item:
            raise MACError("--arms entries must use arm=weight")
        name, raw_weight = item.split("=", 1)
        name = name.strip()
        if not name:
            raise MACError("--arms contains an empty arm name")
        try:
            weights[name] = float(raw_weight)
        except ValueError as exc:
            raise MACError("invalid weight for review arm %s" % name) from exc
    if not weights:
        raise MACError("--arms requires at least one arm=weight entry")
    return weights


def _read_text_arg(
    inline: Optional[str],
    file_path: Optional[str],
    *,
    label: str,
    default: str = "",
) -> str:
    """Resolve a text-bearing CLI argument from one of: inline string,
    file path, or '-' for stdin. Lets callers avoid shell-quoting
    hazards on multi-line / metacharacter-heavy values (parens, braces,
    backticks, ``$``).

    Inline value wins when both are supplied. ``--<arg>-file -`` reads
    from stdin so pipes and heredocs work without quoting.
    """
    import sys
    if inline is not None and inline != "":
        return inline
    if file_path:
        if file_path == "-":
            return sys.stdin.read()
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            raise SystemExit("failed to read %s from %s: %s" % (label, file_path, exc))
    return default


def _read_json_arg(
    inline: Optional[str],
    file_path: Optional[str],
    *,
    label: str,
    default: Any,
) -> Any:
    raw = _read_text_arg(inline, file_path, label=label, default="")
    if not raw or not raw.strip():
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit("invalid JSON in %s: %s" % (label, exc))


# Output mode. Text (human-readable one-liners) is the DEFAULT; the global
# --json flag switches every command to JSON. Set from main().
_OUTPUT_JSON = False

# Short-id mode. When False (the default), task list lines show a short unique
# prefix (task_ + 8 hex chars, git-style). --full-ids restores 37-char ids.
_FULL_IDS = False


def _set_output_json(enabled: bool) -> None:
    global _OUTPUT_JSON
    _OUTPUT_JSON = bool(enabled)


def _set_full_ids(enabled: bool) -> None:
    global _FULL_IDS
    _FULL_IDS = bool(enabled)


def _unwrap(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    return to_dict() if callable(to_dict) else value


def _trunc(text: Any, n: int = 72) -> str:
    s = "" if text is None else str(text).replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


_TASK_ID_PREFIX = "task_"
_SHORT_HEX_LEN = 8  # git-style: 8 hex digits unique enough for human display

# Human task-list presentation.  The icon remains useful when color is
# disabled (redirected output, NO_COLOR, TERM=dumb), while the ANSI style makes
# state changes scannable in an interactive terminal.
_TASK_STATE_STYLES = {
    "running": ("●", "1;36"),
    "claimed": ("◐", "36"),
    "reviewing": ("◆", "1;35"),
    "needs_review": ("◇", "35"),
    "waiting": ("◷", "36"),
    "blocked": ("!", "1;33"),
    "failed": ("×", "1;31"),
    "open": ("○", "1;34"),
    "completed": ("✓", "32"),
    "cancelled": ("–", "2"),
}
_TASK_STATE_ORDER = {
    state: index
    for index, state in enumerate(
        (
            "running",
            "claimed",
            "reviewing",
            "needs_review",
            "waiting",
            "blocked",
            "failed",
            "open",
            "completed",
            "cancelled",
        )
    )
}


def _terminal_color_enabled(
    stream: Any = None,
    environ: Optional[Mapping[str, str]] = None,
) -> bool:
    """Return whether human CLI output should contain ANSI color.

    Color is automatic for a TTY, disabled for pipes and ``TERM=dumb``, and
    honors the de-facto ``NO_COLOR`` contract. ``FORCE_COLOR`` and
    ``CLICOLOR_FORCE`` are useful for snapshots and terminal wrappers; an
    explicit ``0`` disables color.
    """
    env = os.environ if environ is None else environ
    if "NO_COLOR" in env:
        return False
    force = str(env.get("FORCE_COLOR") or env.get("CLICOLOR_FORCE") or "").strip()
    if force:
        return force != "0"
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    return bool(callable(isatty) and isatty() and env.get("TERM", "") != "dumb")


def _ansi(text: str, code: str, *, enabled: bool) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if enabled else text


def _task_state_cell(state: Any, width: int, *, color: bool) -> str:
    value = str(state or "?")
    icon, code = _TASK_STATE_STYLES.get(value, ("·", "37"))
    return _ansi(("%s %s" % (icon, value)).ljust(width), code, enabled=color)


def _task_state_group(state: str) -> int:
    if state in {"running", "claimed", "reviewing", "needs_review"}:
        return 0
    if state in {"blocked", "failed"}:
        return 1
    if state == "open":
        return 2
    return 3


def _render_task_table(
    tasks: Iterable[Any],
    *,
    show_project: bool,
    color: Optional[bool] = None,
    width: Optional[int] = None,
) -> str:
    """Render the human ``task list`` view as an adaptive state-sorted table."""
    records = [_unwrap(task) for task in tasks]
    records = [task for task in records if isinstance(task, dict)]
    if not records:
        return "(none)"

    use_color = _terminal_color_enabled() if color is None else bool(color)
    terminal_width = width or shutil.get_terminal_size((120, 24)).columns
    terminal_width = max(60, int(terminal_width))
    display_ids = [
        str(task.get("id") or "")
        if _FULL_IDS
        else _short_task_id(str(task.get("id") or ""))
        for task in records
    ]
    id_width = max(len("TASK"), max(len(task_id) for task_id in display_ids))
    state_width = max(
        len("STATE"),
        max(len(str(task.get("state") or "?")) + 2 for task in records),
    )
    project_width = 0
    if show_project:
        project_width = max(
            len("PROJECT"),
            min(18, max(len(str(task.get("project") or "-")) for task in records)),
        )
    lane_width = max(
        len("LANE"),
        max(len(str(task.get("publication_lane") or "unknown")) for task in records),
    )
    dependency_cells = []
    for task in records:
        dependencies = task.get("dependencies")
        if not isinstance(dependencies, list):
            dependencies = []
        dependency_cells.append(
            "["
            + ",".join(
                dependency_id
                if _FULL_IDS
                else _short_task_id(dependency_id)
                for dependency_id in map(str, dependencies)
            )
            + "]"
        )
    dependencies_width = max(
        len("DEPENDENCIES"),
        max(len(cell) for cell in dependency_cells),
    )
    fixed_width = (
        id_width
        + 2
        + state_width
        + 2
        + lane_width
        + 2
        + dependencies_width
        + 2
    )
    if show_project:
        fixed_width += project_width + 2
    title_width = max(1, terminal_width - fixed_width)

    indexed = list(enumerate(records))
    indexed.sort(
        key=lambda item: (
            _task_state_group(str(item[1].get("state") or "")),
            _TASK_STATE_ORDER.get(str(item[1].get("state") or ""), 999),
            item[0],
        )
    )

    header_parts = [
        "TASK".ljust(id_width),
        "STATE".ljust(state_width),
        "LANE".ljust(lane_width),
        "DEPENDENCIES".ljust(dependencies_width),
    ]
    rule_parts = [
        "─" * id_width,
        "─" * state_width,
        "─" * lane_width,
        "─" * dependencies_width,
    ]
    if show_project:
        header_parts.append("PROJECT".ljust(project_width))
        rule_parts.append("─" * project_width)
    header_parts.append("TITLE")
    rule_parts.append("─" * title_width)
    lines = [
        _ansi("  ".join(header_parts), "1", enabled=use_color),
        _ansi("  ".join(rule_parts), "2", enabled=use_color),
    ]

    previous_group: Optional[int] = None
    counts: Dict[str, int] = {}
    for original_index, task in indexed:
        state = str(task.get("state") or "?")
        group = _task_state_group(state)
        if previous_group is not None and group != previous_group:
            lines.append("")
        previous_group = group
        counts[state] = counts.get(state, 0) + 1

        raw_id = str(task.get("id") or "")
        display_id = raw_id if _FULL_IDS else _short_task_id(raw_id)
        row = [
            _ansi(display_id.ljust(id_width), "2", enabled=use_color),
            _task_state_cell(state, state_width, color=use_color),
            str(task.get("publication_lane") or "unknown").ljust(lane_width),
            dependency_cells[original_index].ljust(dependencies_width),
        ]
        if show_project:
            project = _trunc(task.get("project") or "-", project_width)
            row.append(_ansi(project.ljust(project_width), "36", enabled=use_color))
        row.append(_trunc(task.get("title", ""), title_width))
        lines.append("  ".join(row))

    count_label = "%d task%s" % (len(records), "" if len(records) == 1 else "s")
    summary = [_ansi(count_label, "1", enabled=use_color)]
    ordered_states = sorted(
        counts,
        key=lambda state: (_TASK_STATE_ORDER.get(state, 999), state),
    )
    for state in ordered_states:
        icon, code = _TASK_STATE_STYLES.get(state, ("·", "37"))
        summary.append(
            _ansi("%s %d %s" % (icon, counts[state], state), code, enabled=use_color)
        )
    lines.extend(("", "  ".join(summary)))
    return "\n".join(lines)


def _short_task_id(task_id: str) -> str:
    """Return a compact display id: ``task_`` + first 8 hex chars.

    Falls back to the full id when it is already short (test fixtures / manual
    ids that don't carry the full 32-char hex suffix). The full id is never
    altered in JSON output or internal logic — only the text display column.
    """
    if not task_id.startswith(_TASK_ID_PREFIX):
        return task_id
    hex_part = task_id[len(_TASK_ID_PREFIX):]
    if len(hex_part) <= _SHORT_HEX_LEN:
        return task_id  # already short enough; preserve as-is
    return _TASK_ID_PREFIX + hex_part[:_SHORT_HEX_LEN]


def _agent_hw_summary(d: dict) -> str:
    """Compact measured-hardware string for an agent line, from the
    ``resources.hardware`` block the agent reports at registration/heartbeat
    (``mac.hardware.v1``). "-" when the agent hasn't reported hardware."""
    res = d.get("resources")
    hw = (res.get("hardware") or {}) if isinstance(res, dict) else {}
    if not isinstance(hw, dict) or not hw:
        return "-"
    bits = []
    if hw.get("os"):
        bits.append("%s/%s" % (hw.get("os"), hw.get("arch") or "?"))
    if hw.get("cpu_count"):
        bits.append("%sc" % hw.get("cpu_count"))
    if hw.get("memory_mb"):
        bits.append("%dG" % round(float(hw["memory_mb"]) / 1024))
    gpu = hw.get("gpu") or {}
    name = str(gpu.get("name") or "").replace("NVIDIA ", "").replace("GeForce ", "")
    name = name.replace(" Server Edition", "").replace("Apple ", "")
    if name:
        vram = gpu.get("vram_mb")
        bits.append(name + (" %dG" % round(float(vram) / 1024) if vram else ""))
    accel = hw.get("accelerator")
    # cuda is implied by an NVIDIA gpu name; spell out anything else (metal),
    # or cuda with no name to identify the accelerator at all.
    if accel and (accel != "cuda" or not name):
        bits.append(str(accel))
    return " ".join(bits) or "-"


#: Resolves a task id to its CURRENT state for the blocked-by section. Set by
#: cmd_task_show, because _one_liner is a pure renderer with no control-plane
#: handle of its own and threading one through every call site would be a large
#: change for one line of output.
_LIVE_TASK_STATE: Optional[Callable[[str], Optional[str]]] = None


def _blocking_dependency_lines(
    task: Dict[str, Any], detail: Any = None, live_state: Any = None
) -> List[str]:
    """Name the dependencies holding this task, with their states.

    A blocked task used to render as `dependencies: 1` and a diagnosis reading
    "Task blocked: dependencies_incomplete", whose remediation advised running
    `mac task show` -- the page already on screen. Everything needed was in the
    record: metadata.dependency_resolution.unsatisfied maps each blocking
    dependency to the state it is stuck in.

    Titles are looked up when the detail payload carries them, because an id
    alone still makes the reader run another command to learn anything.
    """
    metadata = task.get("metadata")
    if not isinstance(metadata, dict):
        return []
    resolution = metadata.get("dependency_resolution")
    if not isinstance(resolution, dict):
        return []
    unsatisfied = resolution.get("unsatisfied")
    if not isinstance(unsatisfied, dict) or not unsatisfied:
        return []

    titles: Dict[str, str] = {}
    if isinstance(detail, dict):
        for key in ("dependency_tasks", "dependencies_detail", "blocked_by"):
            for item in detail.get(key) or []:
                if isinstance(item, dict) and item.get("id"):
                    titles[str(item["id"])] = str(item.get("title") or "")

    # The recorded state is a SNAPSHOT from the moment the block was observed,
    # and it goes stale: cancelling a failed blocker leaves this map still
    # saying "failed". That is not just a cosmetic lie -- the advice below
    # branches on the state, so acting on a stale value gives stale advice.
    # Resolve it live when the caller can.
    states: Dict[str, str] = {}
    for dep_id, info in unsatisfied.items():
        recorded = str(info.get("state") or "") if isinstance(info, dict) else ""
        current = ""
        if callable(live_state):
            try:
                current = str(live_state(dep_id) or "")
            except Exception:  # noqa: BLE001 - fall back to the snapshot
                current = ""
        states[dep_id] = current or recorded

    lines = ["  blocked by:"]
    for dep_id, info in sorted(unsatisfied.items()):
        state = states.get(dep_id, "")
        title = titles.get(dep_id, "")
        lines.append(
            "    %s  %s%s"
            % (dep_id if _FULL_IDS else _short_task_id(dep_id), state or "unknown state", ("  " + title[:52]) if title else "")
        )
    # The next step, computed from THIS task's join policy and the blocker's
    # actual state -- not generic prose.
    #
    # An earlier version of this advice said "cancel the dependency if it will
    # never succeed", and that was wrong twice over. `cancel` is not a legal
    # transition from `failed` at all (failed -> open is the only one), and
    # even where it is legal it releases the parent only under an all_settled
    # join; under all_success, the default, a cancelled dependency leaves the
    # parent blocked for ever. Both facts are knowable from the record, so the
    # advice is derived rather than asserted.
    policy = metadata.get("dependency_policy")
    coordination = metadata.get("coordination")
    join = ""
    if isinstance(policy, dict):
        join = str(policy.get("join") or "")
    if not join and isinstance(coordination, dict):
        join = str(coordination.get("join_policy") or "")
    if not join:
        join = (
            "all_settled"
            if isinstance(coordination, dict)
            and coordination.get("mode") == "cooperative_integration"
            else "all_success"
        )

    terminal = [
        dep_id
        for dep_id in sorted(unsatisfied)
        if states.get(dep_id) in {"failed", "cancelled"}
    ]
    lines.append("    join: %s" % join)
    if terminal and join != "all_settled":
        lines.append(
            "    -> under %s only a COMPLETED dependency releases this task." % join
        )
        blocker = terminal[0]
        blocker_state = states.get(blocker, "terminal")
        lines.append(
            "       `mac task reopen %s` to retry it (%s -> open is its only"
            % (
                blocker if _FULL_IDS else _short_task_id(blocker),
                blocker_state,
            )
        )
        lines.append(
            "       legal move). If it can never succeed, this task cannot run"
        )
        lines.append("       either -- cancel this task rather than the blocker.")
    elif terminal:
        lines.append(
            "    -> under all_settled a terminal dependency already satisfies the"
        )
        lines.append("       join; if this task is still blocked, reopen it.")
    else:
        lines.append(
            "    -> fix the dependency above; this task dispatches when it completes"
        )
        lines.append("       (`mac task show <id>` for the blocker)")
    return lines


def _one_liner(value: Any) -> str:
    """A single compact line for one record (task / agent / generic dict).

    Task lines show a short unique id prefix by default (git-style, 8 hex
    chars: e.g. ``task_d95bcaee``). Pass ``--full-ids`` on ``task list`` or
    set ``_FULL_IDS = True`` to restore the 37-char canonical id.
    """
    d = _unwrap(value)
    if not isinstance(d, dict):
        return str(d)
    ident = d.get("id") or d.get("name") or d.get("key") or ""
    is_task = str(d.get("id", "")).startswith("task_") or (
        "state" in d and "status" not in d
    )
    if is_task:
        raw_id = str(ident)
        display_id = raw_id if _FULL_IDS else _short_task_id(raw_id)
        id_width = 36 if _FULL_IDS else 13
        return ("%-*s %-12s %-10s %s" % (
            id_width,
            display_id,
            d.get("state", "?"),
            (d.get("project") or "-"),
            _trunc(d.get("title", "")),
        ))
    if "status" in d and ("name" in d or "current_task_id" in d or "capabilities" in d):
        cur = d.get("current_task_id")
        held = bool(d.get("dispatch_hold"))
        status = "held" if held else d.get("status", "?")
        activity = (
            "hold: " + _trunc(d.get("dispatch_hold_reason", ""), 60)
            if held
            else ("▶ " + str(cur)) if cur else "idle"
        )
        return "%-16s %-9s %-8s %-28s %s" % (
            d.get("name") or ident,
            status,
            str(d.get("instance_kind") or "static"),
            _agent_hw_summary(d),
            activity,
        )
    scal = [
        "%s=%s" % (k, _trunc(v, 40))
        for k, v in d.items()
        if not isinstance(v, (dict, list)) and k not in ("id", "name", "key")
    ]
    line = (str(ident) + ("  " + "  ".join(scal) if scal else "")).strip()
    return line or _trunc(d, 120)


def _task_activity_lines(
    task: Dict[str, Any],
    *,
    include_empty: bool = False,
) -> List[str]:
    """Render the durable, human-authored task activity narrative."""

    metadata = task.get("metadata")
    activity = metadata.get("activity") if isinstance(metadata, dict) else None
    entries = [entry for entry in activity or [] if isinstance(entry, dict)]
    if not entries:
        if not include_empty:
            return []
        return [
            "",
            "(no activity recorded yet — use `mac --json task show` for "
            "structured evidence/logs)",
        ]

    lines = ["", "Activity:"]
    for entry in entries:
        phase = str(entry.get("phase") or "note")
        actor = str(entry.get("actor") or "")
        at = str(entry.get("at") or "")[:19]
        label = (
            phase
            + ((" / " + actor) if actor else "")
            + ((" @ " + at) if at else "")
        )
        lines.append("  • %s" % label)
        for line in str(entry.get("summary") or "").splitlines():
            lines.append("      %s" % line)
    return lines


def _render_text(value: Any) -> str:
    value = _unwrap(value)
    if value is None:
        return "(none)"
    if isinstance(value, list):
        return "\n".join(_one_liner(v) for v in value) if value else "(none)"
    if isinstance(value, dict):
        # Detail wrapper (e.g. `task show` -> {task, evidence, history, reviews,
        # publications}). Show the headline + compact counts, not the full blob.
        if isinstance(value.get("task"), dict):
            t = value["task"]
            lines = [_one_liner(t)]
            publication_route = value.get("publication_route")
            if isinstance(publication_route, dict):
                lane = publication_route.get("lane") or "unknown"
                route_state = publication_route.get("route_state") or ""
                package_id = publication_route.get("package_id")
                suffix = " (%s)" % route_state if route_state else ""
                if package_id:
                    suffix += " package=%s" % package_id
                lines.append("  publication_lane: %s%s" % (lane, suffix))
            for k in ("assignee", "attempt_count", "max_attempts"):
                if t.get(k) not in (None, ""):
                    lines.append("  %s: %s" % (k, t.get(k)))
            for k in ("dependencies", "evidence", "reviews", "publications", "history"):
                v = value.get(k, t.get(k))
                if isinstance(v, list) and v:
                    lines.append("  %s: %d" % (k, len(v)))
            # A blocked task's entire story is WHICH dependency is stuck and in
            # what state. That was rendered as the bare count above --
            # "dependencies: 1" -- while the record carried the id and the
            # state all along, and the reader was then advised to run `mac task
            # show`, the page they were already reading. Name the blockers.
            lines.extend(
                _blocking_dependency_lines(t, value, live_state=_LIVE_TASK_STATE)
            )
            llm_usage = value.get("llm_usage")
            if isinstance(llm_usage, dict):
                route_count = int(llm_usage.get("observed_route_count") or 0)
                models = [str(item) for item in llm_usage.get("resolved_models") or []]
                providers = [str(item) for item in llm_usage.get("providers") or []]
                if route_count:
                    summary = "%s via %s" % (
                        ", ".join(models) or "unknown model",
                        ", ".join(providers) or "unknown provider",
                    )
                    total_tokens = int(llm_usage.get("total_tokens") or 0)
                    lines.append(
                        "  llm: %s (%d route%s, %d tokens)"
                        % (
                            summary,
                            route_count,
                            "" if route_count == 1 else "s",
                            total_tokens,
                        )
                    )
                else:
                    lines.append("  llm: no attributed model calls recorded")
            profile = value.get("profile")
            if isinstance(profile, dict):
                kpis = profile.get("kpis")
                commands = profile.get("commands")
                if isinstance(kpis, dict) and isinstance(commands, dict):
                    lines.append(
                        "  profile: %.1fm model, %.1fm commands, %d provider "
                        "attempts, %d command failures"
                        % (
                            float(kpis.get("model_latency_ms") or 0.0) / 60000.0,
                            float(commands.get("duration_ms") or 0.0) / 60000.0,
                            int(profile.get("provider_attempt_count") or 0),
                            int(commands.get("failure_count") or 0),
                        )
                    )
                signals = profile.get("signals")
                if isinstance(signals, list):
                    for signal in signals:
                        if isinstance(signal, dict):
                            lines.append(
                                "  signal: %s — %s"
                                % (
                                    signal.get("code") or "unknown",
                                    signal.get("detail") or "",
                                )
                            )
            lines.extend(_task_activity_lines(t))
            return "\n".join(lines)
        if str(value.get("id", "")).startswith("task_") or "state" in value or (
            "status" in value and ("name" in value or "current_task_id" in value)
        ):
            return _one_liner(value)
        out = []
        for k, v in value.items():
            if isinstance(v, dict):
                out.append("%s: {%d keys}" % (k, len(v)))
            elif isinstance(v, list):
                out.append("%s: [%d]" % (k, len(v)))
            else:
                out.append("%s: %s" % (k, v))
        return "\n".join(out) if out else "(empty)"
    return str(value)


def _print(value: Any) -> None:
    if hasattr(value, "to_dict"):
        value = value.to_dict()

    def _to_serializable(obj: Any) -> Any:
        # Hub-mode handlers return _Dictish wrappers; a top-level one is
        # unwrapped above, but list/nested results (e.g. `project list` ->
        # list[_Dictish]) reach json.dumps un-unwrapped. Unwrap anything that
        # exposes the .to_dict() contract here so every command serializes.
        to_dict = getattr(obj, "to_dict", None)
        if callable(to_dict):
            return to_dict()
        raise TypeError(
            "Object of type %s is not JSON serializable" % type(obj).__name__
        )

    if _OUTPUT_JSON:
        print(json.dumps(value, indent=2, sort_keys=True, default=_to_serializable))
    else:
        print(_render_text(value))


def _plane(args: argparse.Namespace) -> Any:
    """Return a Dispatch (LocalDispatch or RemoteDispatch).

    Kept under the historical name so existing handlers (``_plane(args).foo()``)
    are unchanged. The actual transport is chosen by
    :func:`mac.dispatch.resolve_dispatch`.
    """
    from mac.dispatch import resolve_dispatch

    return resolve_dispatch(args)


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize the local mac control-plane database."""
    _plane(args)
    _print({"status": "initialized", "db": args.db})


def cmd_config_migrate_env_namespace(args: argparse.Namespace) -> None:
    """mac-g55y: rewrite a flat ~/.mac/.env into fleet-scoped form.

    For every credential that may collide across fleets (MAC_API_TOKEN
    et al.), this appends a fleet-scoped sibling key like
    ``MAC_API_TOKEN__<FLEET>`` with the same value. The legacy flat
    key is preserved unless ``--drop-legacy`` is set, so other
    consumers continue working during the transition.
    """
    from pathlib import Path as _P

    from mac.fleet_env import migrate_env_file

    path = _P(args.env_file).expanduser()
    added, kept = migrate_env_file(path, args.fleet, keep_legacy=not args.drop_legacy)
    _print(
        {
            "env_file": str(path),
            "fleet": args.fleet,
            "added": sorted(added.keys()),
            "kept_legacy": sorted(kept.keys()) if not args.drop_legacy else [],
        }
    )


def _login_profile(args: argparse.Namespace, *, fleet: Optional[str] = None) -> str:
    return str(
        getattr(args, "login_profile", None)
        or getattr(args, "logout_profile", None)
        or getattr(args, "profile", None)
        or fleet
        or "default"
    )


def cmd_login(args: argparse.Namespace) -> None:
    """Authenticate a client and manage its login session."""
    from mac.client_login import (
        ClientLoginError,
        default_client_id,
        login,
        login_status,
        renew_login,
        resolve_login_spec,
    )
    from mac.client_profiles import ClientProfileError

    fleet = args.login_fleet or args.fleet
    profile = _login_profile(args, fleet=fleet)
    try:
        if args.login_action == "status":
            result = login_status(profile)
        elif args.login_action == "renew":
            result = renew_login(
                profile,
                expires_in=args.expires_in,
                remote_mac=args.remote_mac,
                connect_timeout=args.connect_timeout,
            )
        else:
            spec = resolve_login_spec(
                ssh_target=args.ssh_target,
                fleet=fleet,
                agent=args.agent,
                fleets_config=args.fleets_config,
                ssh_port=args.ssh_port,
                proxy_jump=args.proxy_jump,
                identity_file=args.identity_file,
                known_hosts_file=args.known_hosts_file,
                host_key_fingerprint=args.host_key_fingerprint,
                host_ca=args.host_ca,
                remote_port=args.remote_port,
            )
            result = login(
                spec=spec,
                profile=profile,
                client_id=args.client_id or default_client_id(),
                display_name=args.name or "",
                scopes=_csv(args.scopes),
                capabilities=_csv(args.capabilities),
                expires_in=args.expires_in,
                local_port=args.local_port,
                remote_host=args.remote_host,
                remote_port=args.remote_port or spec.control_port,
                allow_elevated=args.allow_elevated,
                rotate=args.rotate,
                remote_mac=args.remote_mac,
                connect_timeout=args.connect_timeout,
            )
    except (ClientLoginError, ClientProfileError, OSError) as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_logout(args: argparse.Namespace) -> None:
    """Log out the active client and clear its session."""
    from mac.client_login import ClientLoginError, logout
    from mac.client_profiles import ClientProfileError

    try:
        result = logout(
            _login_profile(args),
            revoke=args.revoke,
            remote_mac=args.remote_mac,
            connect_timeout=args.connect_timeout,
        )
    except (ClientLoginError, ClientProfileError, OSError) as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_client_enroll(args: argparse.Namespace) -> None:
    """Mint a scoped client credential on the hub-local SSH trust channel."""

    if not _OUTPUT_JSON:
        raise MACError(
            "client enroll returns a one-time credential; pass --json and pipe "
            "the manifest directly to `mac admin client profile install -`"
        )

    from mac.client_principals import (
        ClientPrincipalError,
        ClientPrincipalStore,
        enrollment_manifest,
    )

    try:
        issued = ClientPrincipalStore(
            Path(args.registry).expanduser() if args.registry else None
        ).enroll(
            args.client_id,
            display_name=args.name or "",
            fleet=args.fleet_name or "",
            profile=args.profile_name or args.client_id,
            scopes=_csv(args.scopes),
            expires_in=args.expires_in,
            api_url=args.api_url,
            ssh_host_key_fingerprint=args.host_key_fingerprint or "",
            ssh_host_ca=args.host_ca or "",
            capabilities=_csv(args.capabilities),
            allow_elevated=args.allow_elevated,
            rotate=args.rotate,
            actor=args.actor,
        )
    except ClientPrincipalError as exc:
        raise MACError(str(exc)) from exc
    _print(enrollment_manifest(issued))


def cmd_client_renew(args: argparse.Namespace) -> None:
    """Renew a client credential and emit its one-time manifest."""
    if not _OUTPUT_JSON:
        raise MACError(
            "client renew returns a one-time credential; pass --json and pipe "
            "the manifest directly to `mac admin client profile install -`"
        )

    from mac.client_principals import (
        ClientPrincipalError,
        ClientPrincipalStore,
        enrollment_manifest,
    )

    try:
        issued = ClientPrincipalStore(
            Path(args.registry).expanduser() if args.registry else None
        ).renew(args.client_id, expires_in=args.expires_in, actor=args.actor)
    except ClientPrincipalError as exc:
        raise MACError(str(exc)) from exc
    _print(enrollment_manifest(issued))


def cmd_client_revoke(args: argparse.Namespace) -> None:
    """Revoke a registered client principal."""
    from mac.client_principals import ClientPrincipalError, ClientPrincipalStore

    try:
        result = ClientPrincipalStore(
            Path(args.registry).expanduser() if args.registry else None
        ).revoke(args.client_id, actor=args.actor)
    except ClientPrincipalError as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_client_list(args: argparse.Namespace) -> None:
    """List registered client principals."""
    from mac.client_principals import ClientPrincipalError, ClientPrincipalStore

    try:
        result = ClientPrincipalStore(
            Path(args.registry).expanduser() if args.registry else None
        ).list()
    except ClientPrincipalError as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_client_profile_install(args: argparse.Namespace) -> None:
    """Install a client enrollment manifest as a local profile."""
    from mac.client_profiles import (
        ClientProfileError,
        install_enrollment_manifest,
        read_manifest,
    )

    try:
        result = install_enrollment_manifest(
            read_manifest(args.manifest),
            profile_override=args.profile_name,
            activate=not args.no_activate,
        )
    except (ClientProfileError, OSError) as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_client_profile_list(args: argparse.Namespace) -> None:
    """List installed client profiles."""
    from mac.client_profiles import ClientProfileError, list_profiles

    try:
        result = list_profiles()
    except ClientProfileError as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_client_profile_show(args: argparse.Namespace) -> None:
    """Show details for a named client profile."""
    from mac.client_profiles import ClientProfileError, show_profile

    try:
        result = show_profile(args.profile_name)
    except ClientProfileError as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_client_profile_activate(args: argparse.Namespace) -> None:
    """Activate the named client profile."""
    from mac.client_profiles import ClientProfileError, activate_profile

    try:
        result = activate_profile(args.profile_name)
    except ClientProfileError as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_client_profile_remove(args: argparse.Namespace) -> None:
    """Remove the named client profile."""
    from mac.client_profiles import ClientProfileError, remove_profile

    try:
        result = remove_profile(args.profile_name)
    except ClientProfileError as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_client_profile_migrate_legacy(args: argparse.Namespace) -> None:
    """Migrate a legacy client profile into the current format."""
    from mac.client_profiles import ClientProfileError, migrate_legacy_profile

    try:
        result = migrate_legacy_profile(
            fleet=args.fleet_name,
            profile=args.profile_name,
            fleets_config=args.fleets_config,
            env_file=args.env_file,
            allow_legacy_admin_token=args.allow_legacy_admin_token,
            activate=not args.no_activate,
        )
    except (ClientProfileError, OSError) as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_fleet_ssh_spec(args: argparse.Namespace) -> None:
    """Resolve and print the SSH connection spec for a fleet."""
    from mac.fleet_ssh import FleetSshError, load_fleet_config, resolve_fleet_ssh

    try:
        spec = resolve_fleet_ssh(
            load_fleet_config(args.fleets_config),
            getattr(args, "fleet_name", None) or args.fleet,
            args.agent,
            port_override=args.ssh_port,
            portable=args.portable,
        )
    except FleetSshError as exc:
        raise MACError(str(exc)) from exc
    _print(spec.to_dict())


def cmd_fleet_backlog_groom_status(args: argparse.Namespace) -> None:
    """Show the backlog groomer's config + last run report (hub read)."""
    cp = _plane(args)
    status = cp.backlog_groom_status()
    _print(status.to_dict() if hasattr(status, "to_dict") else status)


def cmd_fleet_backlog_groom_run(args: argparse.Namespace) -> None:
    """Trigger one immediate grooming pass across opted-in idle repos."""
    cp = _plane(args)
    report = cp.backlog_groom_run()
    _print(report.to_dict() if hasattr(report, "to_dict") else report)


def _backlog_project_metadata(cp: Any, project: str) -> Dict[str, Any]:
    """Return a project record's mutable metadata dict, or error out.

    Backlog grooming targets onboarded projects (those with a ProjectRecord and
    a repository_url); a derived, record-less project cannot be opted in.
    """
    detail = cp.get_project(project)
    data = detail.to_dict() if hasattr(detail, "to_dict") else detail
    record = data.get("record") if isinstance(data, dict) else None
    if not record:
        raise SystemExit(
            "mac: project %r has no project record (onboard it first: "
            "`mac onboard <repo-url> --project %s`)" % (project, project)
        )
    metadata = record.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def cmd_fleet_backlog_groom_enable(args: argparse.Namespace) -> None:
    """Opt a project into autonomous backlog grooming."""
    cp = _plane(args)
    metadata = _backlog_project_metadata(cp, args.project)
    block = dict(metadata.get("backlog_grooming") or {})
    block["enabled"] = True
    if args.backlog_size is not None:
        block["backlog_size"] = args.backlog_size
    if args.min_ready is not None:
        block["min_ready"] = args.min_ready
    if args.capability:
        block["default_capabilities"] = list(args.capability)
    metadata["backlog_grooming"] = block
    cp.update_project(args.project, metadata=metadata, actor="human")
    _print({"project": args.project, "backlog_grooming": block})


def cmd_fleet_backlog_groom_disable(args: argparse.Namespace) -> None:
    """Opt a project out of autonomous backlog grooming."""
    cp = _plane(args)
    metadata = _backlog_project_metadata(cp, args.project)
    block = dict(metadata.get("backlog_grooming") or {})
    block["enabled"] = False
    metadata["backlog_grooming"] = block
    cp.update_project(args.project, metadata=metadata, actor="human")
    _print({"project": args.project, "backlog_grooming": block})


def cmd_fleet_model_selection_status(args: argparse.Namespace) -> None:
    """Show the active/pending powerhouse-model selection + last refresh."""
    cp = _plane(args)
    status = cp.model_selection_status()
    _print(status.to_dict() if hasattr(status, "to_dict") else status)


def cmd_fleet_model_selection_refresh(args: argparse.Namespace) -> None:
    """Trigger an immediate refresh (discover → moderate → select). A swap is
    recorded pending, not adopted, until promoted."""
    cp = _plane(args)
    out = cp.model_selection_refresh()
    _print(out.to_dict() if hasattr(out, "to_dict") else out)


def cmd_fleet_model_selection_promote(args: argparse.Namespace) -> None:
    """Promote the pending model swap to active (operator gate). Routing changes
    only here — never on an unvalidated swap."""
    cp = _plane(args)
    out = cp.model_selection_promote()
    _print(out.to_dict() if hasattr(out, "to_dict") else out)


def cmd_optimizer_status(args: argparse.Namespace) -> None:
    """Print the optimizer status."""
    _print(_plane(args).optimizer_status())


def cmd_optimizer_tick(args: argparse.Namespace) -> None:
    """Advance the optimizer by one processing tick."""
    _print(_plane(args).optimizer_tick())


def cmd_optimizer_policy_create(args: argparse.Namespace) -> None:
    """Create a scientific optimizer policy."""
    parameters = _read_json_arg(
        args.parameters,
        args.parameters_file,
        label="scientific policy parameters",
        default={},
    )
    if not isinstance(parameters, dict):
        raise MACError("scientific policy parameters must be a JSON object")
    description = _read_text_arg(
        args.description,
        args.description_file,
        label="scientific policy description",
    )
    _print(
        _plane(args).create_scientific_policy(
            args.name,
            args.project,
            parameters,
            description=description,
            created_by=args.actor,
        )
    )


def cmd_optimizer_policy_list(args: argparse.Namespace) -> None:
    """List scientific optimizer policies."""
    _print(
        _plane(args).list_scientific_policies(
            project=args.project,
            status=args.status,
        )
    )


def cmd_optimizer_policy_show(args: argparse.Namespace) -> None:
    """Show a scientific optimizer policy."""
    _print(_plane(args).get_scientific_policy(args.policy_id))


def _optimizer_action_reason(args: argparse.Namespace) -> str:
    return _read_text_arg(
        args.reason,
        args.reason_file,
        label="scientific optimizer action reason",
    ).strip()


def cmd_optimizer_policy_promote(args: argparse.Namespace) -> None:
    """Promote a scientific optimizer policy."""
    _print(
        _plane(args).promote_scientific_policy(
            args.policy_id,
            actor=args.actor,
            reason=_optimizer_action_reason(args),
        )
    )


def cmd_optimizer_policy_rollback(args: argparse.Namespace) -> None:
    """Roll back a scientific optimizer policy."""
    _print(
        _plane(args).rollback_scientific_policy(
            args.project,
            args.policy_id,
            actor=args.actor,
            reason=_optimizer_action_reason(args),
        )
    )


def cmd_optimizer_experiment_create(args: argparse.Namespace) -> None:
    """Create a scientific optimizer experiment."""
    hypothesis = _read_text_arg(
        args.hypothesis,
        args.hypothesis_file,
        label="scientific experiment hypothesis",
    ).strip()
    guardrails = _read_json_arg(
        args.guardrails,
        args.guardrails_file,
        label="scientific experiment guardrails",
        default={},
    )
    metadata = _read_json_arg(
        args.metadata,
        args.metadata_file,
        label="scientific experiment metadata",
        default={},
    )
    if not isinstance(guardrails, dict) or not isinstance(metadata, dict):
        raise MACError("scientific experiment guardrails and metadata must be JSON objects")
    _print(
        _plane(args).create_scientific_experiment(
            name=args.name,
            project=args.project,
            hypothesis=hypothesis,
            control_policy_id=args.control_policy_id,
            treatment_policy_id=args.treatment_policy_id,
            primary_metric=args.primary_metric,
            direction=args.direction,
            min_effect=args.min_effect,
            quality_margin=args.quality_margin,
            min_samples_per_arm=args.min_samples_per_arm,
            max_samples_per_arm=args.max_samples_per_arm,
            exploration_fraction=args.exploration_fraction,
            outcome_horizon_seconds=args.outcome_horizon_seconds,
            guardrails=guardrails,
            auto_promote=args.auto_promote,
            metadata=metadata,
            created_by=args.actor,
        )
    )


def cmd_optimizer_experiment_list(args: argparse.Namespace) -> None:
    """List scientific optimizer experiments."""
    _print(
        _plane(args).list_scientific_experiments(
            project=args.project,
            state=args.state,
        )
    )


def cmd_optimizer_experiment_show(args: argparse.Namespace) -> None:
    """Show a scientific optimizer experiment."""
    _print(_plane(args).get_scientific_experiment(args.experiment_id))


def cmd_optimizer_experiment_evidence(args: argparse.Namespace) -> None:
    """Show evidence recorded for a scientific experiment."""
    _print(
        _plane(args).scientific_experiment_evidence(
            args.experiment_id,
            limit=args.limit,
        )
    )


def cmd_optimizer_experiment_start(args: argparse.Namespace) -> None:
    """Start a scientific optimizer experiment."""
    _print(
        _plane(args).start_scientific_experiment(
            args.experiment_id,
            actor=args.actor,
        )
    )


def cmd_optimizer_experiment_pause(args: argparse.Namespace) -> None:
    """Pause a scientific optimizer experiment."""
    _print(
        _plane(args).pause_scientific_experiment(
            args.experiment_id,
            actor=args.actor,
            reason=_optimizer_action_reason(args),
        )
    )


def cmd_optimizer_experiment_promote(args: argparse.Namespace) -> None:
    """Promote a scientific optimizer experiment."""
    _print(
        _plane(args).promote_scientific_experiment(
            args.experiment_id,
            actor=args.actor,
            reason=_optimizer_action_reason(args),
        )
    )


def cmd_optimizer_experiment_observe(args: argparse.Namespace) -> None:
    """Record an observation for a scientific experiment."""
    _print(
        _plane(args).observe_scientific_task(
            args.experiment_id,
            args.task_id,
        )
    )


def cmd_optimizer_experiment_analyze(args: argparse.Namespace) -> None:
    """Analyze a scientific optimizer experiment."""
    _print(_plane(args).analyze_scientific_experiment(args.experiment_id))


def cmd_fleet_creds_status(args: argparse.Namespace) -> None:
    """Per-agent coding-CLI auth status from the agents' heartbeat reports.

    Each worker re-probes claude/codex/cursor on its command-inventory cycle
    and embeds the secret-free result in resources["coding_clis"], so this is
    a pure hub read: no SSH, no secrets. Agents whose CLIs are on PATH but
    unauthenticated are flagged NEEDS SYNC — run `mac admin fleet creds-sync` from
    the workstation that holds the freshest logins (usually the one you're
    on: you can only be interactive in one place, and that place has the
    newest tokens)."""
    from mac.cli_credentials import KNOWN_CLIS, agent_cli_status, agents_needing_sync

    cp = _plane(args)
    agents = [a.to_dict() if hasattr(a, "to_dict") else dict(a) for a in cp.list_agents()]
    needing = agents_needing_sync(agents)
    rows = []
    for agent in sorted(agents, key=lambda a: str(a.get("name") or "")):
        name = str(agent.get("name") or "")
        status = agent_cli_status(agent.get("resources") or {})
        if not status:
            rows.append({"agent": name, "status": "(no coding_clis report yet — worker predates this feature or has not refreshed)"})
            continue
        summary = {}
        report_schema = str(
            ((agent.get("resources") or {}).get("coding_clis") or {}).get("schema")
            or ""
        )
        is_v2 = report_schema == "mac.coding_clis.v2"
        for cli in KNOWN_CLIS:
            info = status.get(cli) if isinstance(status.get(cli), dict) else {}
            if is_v2 and info.get("verified"):
                summary[cli] = "verified (%s/%s)" % (
                    info.get("provider") or "provider",
                    info.get("protocol") or "protocol",
                )
            elif is_v2 and info.get("configured"):
                # On PATH + credentialed but no same-environment executable
                # proof: the sandbox cannot (yet) launch it, so it is never "ok".
                failure = ((info.get("verification") or {}).get("failure_class") or "unverified")
                summary[cli] = "ROUTE UNAVAILABLE (%s)" % failure
            elif not is_v2 and info.get("available"):
                # Legacy v1 report: "available" was inventory-only. v2 workers
                # gate "available" on the executable probe, so this branch only
                # covers pre-v2 heartbeats.
                summary[cli] = "ok (%s)" % (info.get("auth_source") or "authed")
            elif info.get("on_path"):
                summary[cli] = "NEEDS SYNC"
            else:
                summary[cli] = "not installed"
        rows.append({"agent": name, **summary})
    _print({"agents": rows, "needs_sync": needing})
    if needing:
        print(
            "\nmac: %d agent(s) need coding-CLI credentials. From the workstation "
            "with your freshest logins run:\n  mac admin fleet creds-sync --fleet <fleet>"
            % len(needing),
            file=sys.stderr,
        )


def cmd_fleet_github_ingest_status(args: argparse.Namespace) -> None:
    """Show the GitHub-issue ingestor's config + last run report (hub read)."""
    cp = _plane(args)
    status = cp.github_ingest_status()
    _print(status.to_dict() if hasattr(status, "to_dict") else status)


def cmd_fleet_github_ingest_run(args: argparse.Namespace) -> None:
    """Trigger one immediate ingestion pass across all opted-in repos."""
    cp = _plane(args)
    report = cp.github_ingest_run()
    _print(report.to_dict() if hasattr(report, "to_dict") else report)


def _project_record_metadata(cp: Any, project: str) -> Dict[str, Any]:
    """Return the mutable metadata dict of a project's record, or error out.

    Issue ingestion targets onboarded projects (those with a ProjectRecord and
    a repository_url); a derived, record-less project cannot be opted in.
    """
    detail = cp.get_project(project)
    data = detail.to_dict() if hasattr(detail, "to_dict") else detail
    record = data.get("record") if isinstance(data, dict) else None
    if not record:
        raise SystemExit(
            "mac: project %r has no project record (onboard it first: "
            "`mac onboard <repo-url> --project %s`)" % (project, project)
        )
    metadata = record.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def cmd_project_ingest_enable(args: argparse.Namespace) -> None:
    """Opt a project into GitHub-issue ingestion (merges its metadata block)."""
    cp = _plane(args)
    metadata = _project_record_metadata(cp, args.project)
    block = dict(metadata.get("github_issue_ingest") or {})
    block["enabled"] = True
    if args.label:
        block["labels"] = list(args.label)
    if args.capability:
        block["default_capabilities"] = list(args.capability)
    if args.auto_cancel_closed:
        block["auto_cancel_closed"] = True
    metadata["github_issue_ingest"] = block
    cp.update_project(args.project, metadata=metadata, actor="human")
    _print({"project": args.project, "github_issue_ingest": block})


def cmd_project_ingest_disable(args: argparse.Namespace) -> None:
    """Opt a project out of GitHub-issue ingestion (keeps its config block)."""
    cp = _plane(args)
    metadata = _project_record_metadata(cp, args.project)
    block = dict(metadata.get("github_issue_ingest") or {})
    block["enabled"] = False
    metadata["github_issue_ingest"] = block
    cp.update_project(args.project, metadata=metadata, actor="human")
    _print({"project": args.project, "github_issue_ingest": block})


def cmd_fleet_creds_sync(args: argparse.Namespace) -> None:
    """Push this workstation's coding-CLI credentials to workers, on demand.

    Source of truth is the CURRENT environment: env keys, portable credential
    files in $HOME, or the macOS Keychain. Secrets travel only over the fleet
    SSH routes via stdin — never argv/env/stdout and never through the hub
    ledger — and every push is verified by re-running the worker's own
    detector and printing its secret-free verdict."""
    from mac.cli_credentials import (
        KNOWN_CLIS,
        agents_needing_sync,
        build_sync_manifest,
        detect_local_credentials,
        sync_agent,
    )

    clis = [c.strip() for c in str(args.cli or "").split(",") if c.strip()]
    for cli in clis:
        if cli not in KNOWN_CLIS:
            raise MACError("unknown coding CLI %r (known: %s)" % (cli, ", ".join(KNOWN_CLIS)))
    sources = detect_local_credentials(clis)
    portable = {cli: s for cli, s in sources.items() if s.present}
    for cli in clis:
        source = sources.get(cli)
        if source and source.present:
            print("mac: %s credentials from %s" % (cli, source.origin), file=sys.stderr)
        else:
            print(
                "mac: no portable %s credentials on this workstation (log in to the "
                "CLI here first, or set its API-key env var)" % cli,
                file=sys.stderr,
            )
    if not portable:
        raise MACError("nothing to sync: this workstation holds no portable credentials")

    targets = list(args.agent or [])
    if not targets:
        # Lazy by default: only agents whose own reports say a CLI is present
        # but unauthenticated. Credentials are never pushed where not needed.
        # Hub resolution: honor an explicit authority (--db/--hub-url/global
        # --fleet); otherwise reach the hub of the fleet being synced.
        if not (getattr(args, "db", None) or getattr(args, "hub_url", None) or getattr(args, "fleet", None)):
            args.fleet = args.creds_fleet
        cp = _plane(args)
        agents = [a.to_dict() if hasattr(a, "to_dict") else dict(a) for a in cp.list_agents()]
        needing = agents_needing_sync(agents, clis=list(portable))
        targets = sorted(needing)
        if not targets:
            print(
                "mac: no agent reports a needed sync (pass --agent NAME to force one)",
                file=sys.stderr,
            )
            return
        print(
            "mac: syncing agents that reported missing auth: %s" % ", ".join(targets),
            file=sys.stderr,
        )

    manifest = build_sync_manifest(portable)
    if args.dry_run:
        _print(
            {
                "dry_run": True,
                "agents": targets,
                "clis": sorted(portable),
                "files": sorted((manifest.get("files") or {}).keys()),
                "env_keys": sorted((manifest.get("env") or {}).keys()),
            }
        )
        return
    results = {}
    for agent in targets:
        try:
            verdict = sync_agent(
                args.creds_fleet, agent, manifest, fleets_config=args.fleets_config
            )
            results[agent] = {
                cli: ("ok" if (verdict.get(cli) or {}).get("available") else str((verdict.get(cli) or {}).get("detail") or "unverified"))
                for cli in sorted(portable)
            }
        except Exception as exc:  # noqa: BLE001 - report per-agent, keep going
            results[agent] = {"error": str(exc)}
    _print({"synced": results})


def cmd_fleet_sync_token(args: argparse.Namespace) -> None:
    """auth-token-sync-01: pull the hub's current bearer token into this client.

    The hub accepts only the tokens in its own ~/.mac/mac.env; the client sends
    MAC_API_TOKEN__<FLEET>. When they drift the hub returns 403 "unknown bearer
    token". This re-syncs the client from the authoritative source (the hub host,
    reached out-of-band over SSH).
    """
    from mac.fleet_creds import sync_token

    _print(
        sync_token(
            args.fleet,
            fleets_config_path=args.fleets_config,
            env_path=args.env_file,
        )
    )


def cmd_fleet_rotate_token(args: argparse.Namespace) -> None:
    """auth-token-sync-01: graceful bearer-token rotation via MAC_API_TOKENS.

    Default is a dry-run plan. --apply adds a new token alongside the old
    (overlap window) and advertises it as the new primary; --prune --apply
    drops the old tokens once every client has rolled over via sync-token.
    """
    from mac.fleet_creds import rotate_token

    _print(
        rotate_token(
            args.fleet,
            scopes=tuple(args.scope) if args.scope else ("admin",),
            prune=args.prune,
            do_apply=args.apply,
            restart=args.restart,
            fleets_config_path=args.fleets_config,
            env_path=args.env_file,
        )
    )


def cmd_tenant_register(args: argparse.Namespace) -> None:
    """Register a new tenant."""
    _print(
        _plane(args).register_tenant(
            args.name,
            metadata=_json_arg(args.metadata, {}),
            tenant_id=args.tenant_id,
        )
    )


def cmd_tenant_list(args: argparse.Namespace) -> None:
    """List registered tenants."""
    _print([tenant.to_dict() for tenant in _plane(args).list_tenants()])


def cmd_user_register(args: argparse.Namespace) -> None:
    """Register a user within a tenant."""
    _print(
        _plane(args).register_user(
            args.tenant_id,
            args.handle,
            display_name=args.display_name or "",
            metadata=_json_arg(args.metadata, {}),
            user_id=args.user_id,
        )
    )


def cmd_persona_register(args: argparse.Namespace) -> None:
    """Register a persona for a tenant."""
    _print(
        _plane(args).register_persona(
            args.tenant_id,
            args.name,
            args.soul_ref,
            args.memory_scope,
            metadata=_json_arg(args.metadata, {}),
            persona_id=args.persona_id,
        )
    )


def cmd_human_interface_port(args: argparse.Namespace) -> None:
    """Port an agent's profile between Hermes and OpenClaw.

    Local by definition: an agent's identity documents and messaging
    credentials live in its home directory, so this reads and writes the
    filesystem rather than the hub.
    """
    from mac.human_interface_profile import port_profile

    _print(
        port_profile(
            args.source,
            args.target,
            home=Path(args.home) if getattr(args, "home", None) else None,
            dry_run=not args.apply,
            state_file=(
                Path(args.state_file) if getattr(args, "state_file", None) else None
            ),
        )
    )


def cmd_human_interface_check(args: argparse.Namespace) -> None:
    """Report whether switching to an interface would lose the agent's profile."""
    from mac.human_interface_profile import assert_switch_ported, switch_readiness

    if args.assert_ready:
        # Raises ProfilePortError naming the fix; the CLI's error path turns
        # that into a non-zero exit, which is what a deploy gate needs.
        _print(
            assert_switch_ported(
                args.target,
                home=Path(args.home) if getattr(args, "home", None) else None,
                state_file=(
                    Path(args.state_file) if getattr(args, "state_file", None) else None
                ),
                max_age_seconds=args.max_age_seconds,
            )
        )
        return
    _print(
        switch_readiness(
            args.target,
            home=Path(args.home) if getattr(args, "home", None) else None,
            state_file=(
                Path(args.state_file) if getattr(args, "state_file", None) else None
            ),
            max_age_seconds=args.max_age_seconds,
        )
    )


def cmd_hermes_register(args: argparse.Namespace) -> None:
    """Register a Hermes persona instance for a tenant."""
    _print(
        _plane(args).register_persona_instance(
            args.tenant_id,
            args.name,
            persona_id=args.persona_id,
            home_ref=args.home_ref or "",
            status=args.status,
            metadata=_json_arg(args.metadata, {}),
            instance_id=args.instance_id,
        )
    )


def cmd_hermes_context(args: argparse.Namespace) -> None:
    """Show the context for a persona instance."""
    _print(_plane(args).persona_context(args.instance_id))


def cmd_hermes_work_context(args: argparse.Namespace) -> None:
    """Show the work context for a persona instance."""
    _print(
        _plane(args).persona_work_context(
            args.instance_id,
            include_completed=not args.active_only,
            task_limit=args.task_limit,
        )
    )


def cmd_hermes_runtime_proof(args: argparse.Namespace) -> None:
    """Print Hermes runtime proof and startup diagnostics."""
    startup = None
    if not args.skip_startup_report:
        from mac.hermes_startup import build_hermes_startup_report

        startup = build_hermes_startup_report()
    _print(_plane(args).persona_runtime_proof(args.instance_id, hermes_startup=startup))


def cmd_binding_register(args: argparse.Namespace) -> None:
    """Register a platform binding for a persona instance."""
    _print(
        _plane(args).register_platform_binding(
            args.tenant_id,
            args.persona_instance_id,
            args.platform,
            args.external_id,
            display_name=args.display_name or "",
            scopes=_json_arg(args.scopes, {}),
            metadata=_json_arg(args.metadata, {}),
            binding_id=args.binding_id,
        )
    )


def cmd_interaction_task(args: argparse.Namespace) -> None:
    """Create an interaction task for a persona instance."""
    _print(
        _plane(args).create_interaction_task(
            args.persona_instance_id,
            args.title,
            user_id=args.user_id,
            platform_binding_id=args.platform_binding_id,
            conversation_ref=args.conversation_ref,
            description=args.description or "",
            project=args.project,
            priority=args.priority,
            required_capabilities=_csv(args.required_capabilities),
            dependencies=_csv(args.dependencies),
            metadata=_json_arg(args.metadata, {}),
            max_attempts=args.max_attempts,
            actor=args.actor,
        )
    )


def cmd_task_migrate_beads(args: argparse.Namespace) -> None:
    """Migrate legacy Beads memories and tickets into tasks."""
    from pathlib import Path as _Path
    from mac.beads_migrator import migrate, read_beads_memories_via_cli

    repo_path = _Path(args.repo_path).expanduser().resolve()
    if args.tickets_only:
        cp = None
        memories = None
    else:
        cp = _plane(args)
        memories = {} if args.no_memories else read_beads_memories_via_cli(repo_path)
    report = migrate(
        repo_path,
        cp,
        project=args.project,
        actor=args.actor,
        dry_run=args.dry_run,
        emit_tickets=not args.no_tickets,
        memories=memories,
        tickets_only=args.tickets_only,
    )
    _print(report.to_dict())


def cmd_task_detect_beads(args: argparse.Namespace) -> None:
    """Detect Beads artifacts in a repository."""
    from pathlib import Path as _Path
    from dataclasses import asdict
    from mac.beads_migrator import detect

    _print(asdict(detect(_Path(args.repo_path).expanduser().resolve())))


def cmd_task_detect_ticketing(args: argparse.Namespace) -> None:
    """Connector-aware detection: which ticketing sources a repo has + whether a
    one-way ledger import should be offered."""
    _print(_plane(args).detect_ticketing(args.repo_path))


def cmd_task_convert_ticketing(args: argparse.Namespace) -> None:
    """Run the one-way conversion of a detected foreign source (e.g. beads) into
    MAC ledger tasks plus optional local compatibility files."""
    _print(
        _plane(args).convert_ticketing_source(
            args.repo_path, project=args.project, actor=args.actor, dry_run=args.dry_run
        )
    )


def _default_project_from_cwd() -> Optional[str]:
    """Infer a task's project from the working directory (bd parity).

    `bd` tagged new issues with the repo they were filed from; `mac task
    create` historically left ``project`` null unless ``--project`` was passed,
    so work filed from a checkout never showed up under that project. Use the
    git top-level directory name (works from any subdirectory of a checkout),
    falling back to the cwd basename outside git. Returns None when nothing
    sensible can be derived.
    """
    import subprocess

    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        root = top.stdout.strip()
        if top.returncode == 0 and root:
            return os.path.basename(root) or None
    except Exception:
        pass
    try:
        return os.path.basename(os.getcwd()) or None
    except OSError:
        return None


def _effective_read_project(args: argparse.Namespace) -> Optional[str]:
    """Project filter for read commands (list/ready/search), bd parity.

    ``--all`` clears the filter (every project); ``--project NAME`` picks one;
    otherwise default to the working directory's project. Returns None to mean
    "every project".
    """
    if getattr(args, "all", False):
        return None
    proj = getattr(args, "project", None)
    if proj is not None:
        return proj or None
    inferred = _default_project_from_cwd()
    if inferred:
        print(
            "mac: scoping to project %r (use --all for every project, "
            "--project NAME to choose)" % inferred,
            file=sys.stderr,
        )
    return inferred


def _maybe_emit_ticket(
    result: Any, args: argparse.Namespace, *, close_reason: Optional[str] = None
) -> None:
    """Mirror a created/closed task to ``.tickets/<id>.md`` (parity-tickets-autoemit-01).

    Best-effort and opt-out (``--no-ticket`` / ``MAC_NO_TICKET_MIRROR``); never
    fails the command if the mirror can't be written.
    """
    if getattr(args, "no_ticket", False):
        return
    data = result.to_dict() if hasattr(result, "to_dict") else result
    try:
        from mac.tickets_mirror import emit

        path = emit(data, close_reason=close_reason)
    except Exception:  # noqa: BLE001 - the mirror is a convenience, not the operation
        return
    if path:
        print("mac: wrote ticket mirror %s" % path, file=sys.stderr)


def cmd_task_create(args: argparse.Namespace) -> None:
    """Create a task."""
    cp = _plane(args)
    description = _read_text_arg(
        args.description,
        getattr(args, "description_file", None),
        label="--description",
        default="",
    )
    metadata = _read_json_arg(
        args.metadata,
        getattr(args, "metadata_file", None),
        label="--metadata",
        default={},
    )
    if getattr(args, "no_dispatch", False):
        # Stage the task: the loop-mode fleet won't auto-claim it (and it's
        # hidden from `task ready`) until an operator starts it explicitly.
        metadata["no_dispatch"] = True
    if getattr(args, "sync", False):
        # A barrier on one worker: it waits for that worker to drain, runs
        # alone, and holds off new async work while it does.
        metadata["execution_mode"] = "sync"
    target_agent = str(getattr(args, "target_agent", "") or "").strip()
    if target_agent:
        metadata["target_agent_id"] = target_agent
    if getattr(args, "no_decompose", False):
        # Handoff / plan-note guard: the executor will not auto-decompose this
        # task into child tasks (add_child_tasks refuses with no_decompose).
        metadata["no_decompose"] = True
    publication_lane = str(
        getattr(args, "publication_lane", "auto") or "auto"
    ).strip().lower()
    if publication_lane != "auto":
        metadata["publication_lane_policy"] = publication_lane
    if publication_lane == "managed":
        # Managed single-task admission is an atomic-work assertion.  Keep the
        # CLI ergonomic while the control plane still performs every structural
        # and rollout check before it creates held package state.
        metadata["no_decompose"] = True
    model = str(getattr(args, "model", "") or "").strip()
    if model:
        # Per-task LLM pin by NAME: worker exports MAC_TASK_MODEL to the
        # executor, which maps it to the runtime/CLI model flag for this run.
        metadata["model"] = model
    strength = getattr(args, "model_strength", None)
    if strength is not None:
        # Name-decoupled pin: 1 = cheapest/weakest .. 10 = strongest. Resolved
        # to a concrete available model at run time via the strength ladder, so
        # the task stays valid as model names churn. --model wins if both given.
        if not 1 <= int(strength) <= 10:
            raise MACError("--model-strength must be an integer 1..10")
        metadata["model_strength"] = int(strength)
    kind = normalize_deliverable_kind(getattr(args, "kind", ""))
    if kind == REPORT_DELIVERABLE:
        # Non-code deliverable: the fleet won't demand a repo diff/pushed
        # branch; a substantive operator_result (summary/findings/artifacts)
        # satisfies verification. Lets investigation/answer tasks run without
        # faking a code change — and makes system-smoke tasks trivial without
        # a test-only bypass of the substance gate.
        metadata["deliverable"] = REPORT_DELIVERABLE
    elif kind:
        raise MACError(
            "unknown --kind %r (use 'code' or 'report' / answer / analysis / "
            "investigation / question / triage)" % args.kind
        )
    # bd parity: when --project is omitted, tag the task with the working
    # directory's project (git repo name, else cwd basename). Pass an explicit
    # --project (including --project '' for none) to override.
    project = args.project
    if project is None:
        project = _default_project_from_cwd()
        if project:
            print(
                "mac: tagging task with project %r (inferred from cwd; "
                "pass --project to override, --project '' for none)" % project,
                file=sys.stderr,
            )
    project = project or None
    create_kwargs = {}
    idempotency_key = str(
        getattr(args, "idempotency_key", "") or ""
    ).strip()
    if idempotency_key:
        create_kwargs["idempotency_key"] = idempotency_key
    if publication_lane == "legacy" and hasattr(cp, "store"):
        # A local ledger is an explicit operator authority. Remote hubs enforce
        # the same downgrade through their authenticated admin principal.
        create_kwargs["_allow_legacy_publication"] = True
    created = cp.create_task(
        args.title,
        description=description,
        project=project,
        priority=args.priority,
        required_capabilities=_csv(args.required_capabilities),
        dependencies=_csv(args.dependencies),
        metadata=metadata,
        max_attempts=args.max_attempts,
        actor=args.actor,
        **create_kwargs,
    )
    _maybe_emit_ticket(created, args)
    payload = created.to_dict() if hasattr(created, "to_dict") else dict(created)
    route = payload.get("publication_route")
    if not isinstance(route, dict):
        route = {
            "schema": "mac.task_publication_route.unreported.v1",
            "lane": str(payload.get("publication_lane") or "unknown"),
            "route_state": "unreported",
        }
        try:
            route_value = cp.task_publication_route(payload["id"])
            route = (
                route_value.to_dict()
                if hasattr(route_value, "to_dict")
                else dict(route_value)
            )
        except Exception:  # noqa: BLE001 - mixed-version hub compatibility
            pass
    payload["publication_lane"] = route.get("lane") or "unknown"
    payload["publication_route"] = route
    _print(payload)


def _apply_selector(records: List[Any], args: argparse.Namespace, object_name: str) -> List[Any]:
    """Narrow `records` by --selector, if one was given.

    One grammar for every first-class object: the same expression that names a
    group of tasks names a group of agents. An unknown key is REFUSED with that
    object's valid keys rather than silently dropped -- a dropped term widens
    the group, and on a mutating verb the group is what is about to change.
    """
    expression = getattr(args, "selector", None)
    if not expression:
        return records
    from mac.object_selection import filter_records

    return filter_records(records, expression, object_name)


def _selector_guarded_mutation(
    records: List[Any],
    args: argparse.Namespace,
    object_name: str,
    verb: str,
) -> Optional[List[Any]]:
    """Report what a selector-driven mutation would touch; None means stop.

    DRY BY DEFAULT, deliberately. The reason to want selectors on a mutating
    verb is exact -- "the only way to clean what may be thousands of bad
    tasks" -- and it is the same reason it must not fire on a typo. A selector
    that narrows too far costs a re-run; one that widens too far costs the
    ledger.

    Without --apply this prints the count and a sample and changes nothing.
    The preview and the apply evaluate the SAME expression, so what was shown
    is what runs.
    """
    selected = _apply_selector(records, args, object_name)
    if not getattr(args, "apply", False):
        noun = object_name + ("" if len(selected) == 1 else "s")
        print(
            "%d %s would be %s by selector %r"
            % (len(selected), noun, verb, getattr(args, "selector", "")),
            file=sys.stderr,
        )
        for record in selected[:10]:
            print("  %s" % _one_liner(record), file=sys.stderr)
        if len(selected) > 10:
            print("  ... and %d more" % (len(selected) - 10), file=sys.stderr)
        print("Nothing changed. Re-run with --apply to do it.", file=sys.stderr)
        return None
    return selected


def cmd_task_list(args: argparse.Namespace) -> None:
    """List tasks for the effective project."""
    cp = _plane(args)
    project = _effective_read_project(args)
    # An explicit --selector means "I am naming the group". The IMPLICIT cwd
    # project scope must not silently shrink it: `task list --selector
    # 'project=nanolang'` run from another directory found nothing, which reads
    # as "there is none" rather than "you were scoped elsewhere". An explicit
    # --project still wins, because that is the operator saying it twice.
    if getattr(args, "selector", None) and not getattr(args, "project", None):
        project = None
    limit = getattr(args, "limit", None) or None
    tasks = [
        task.to_dict()
        for task in cp.list_tasks(
            args.state,
            project=project,
            limit=limit,
            view="summary",
        )
    ]
    missing_routes = [
        task["id"] for task in tasks if not isinstance(task.get("publication_route"), dict)
    ]
    if missing_routes and hasattr(cp, "task_publication_routes"):
        try:
            routes = cp.task_publication_routes(missing_routes)
            for task in tasks:
                if task["id"] in routes:
                    task["publication_route"] = routes[task["id"]]
                    task["publication_lane"] = routes[task["id"]]["lane"]
        except Exception:  # noqa: BLE001 - mixed-version hub compatibility
            pass
    # Narrow by --selector before rendering, so the table and the JSON agree.
    tasks = _apply_selector(tasks, args, "task")
    # Short-id display is text-only. JSON always retains canonical full ids.
    _set_full_ids(bool(getattr(args, "full_ids", False)))
    try:
        if _OUTPUT_JSON:
            _print(tasks)
        else:
            print(_render_task_table(tasks, show_project=project is None))
    finally:
        _set_full_ids(False)  # reset to default after this command


def cmd_task_show(args: argparse.Namespace) -> None:
    """Show details for a task."""
    cp = _plane(args)
    # Let the blocked-by section report each blocker's CURRENT state rather
    # than the snapshot taken when the block was recorded. Cheap: only blocked
    # tasks have blockers, and there are usually one or two.
    global _LIVE_TASK_STATE
    _LIVE_TASK_STATE = lambda dep_id: _task_state(cp, dep_id)
    try:
        _print(cp.task_detail(args.task_id))
    finally:
        _LIVE_TASK_STATE = None


def cmd_database_migrate_sqlite_postgres(args: argparse.Namespace) -> None:
    """Perform an offline, fully verified authority migration."""
    if not args.hub_stopped:
        raise MACError(
            "offline authority migration requires --hub-stopped after every "
            "hub writer and worker supervisor has been stopped"
        )
    dsn = str(args.postgres_url or "").strip()
    if args.postgres_url_file:
        dsn = (
            Path(args.postgres_url_file)
            .expanduser()
            .read_text(encoding="utf-8")
            .strip()
        )
    if not dsn:
        dsn = (
            os.environ.get("MAC_MIGRATION_DATABASE_URL", "").strip()
            or os.environ.get("MAC_DATABASE_URL", "").strip()
        )
    if not dsn:
        raise MACError(
            "PostgreSQL target is required via --postgres-url, "
            "--postgres-url-file, MAC_MIGRATION_DATABASE_URL, or MAC_DATABASE_URL"
        )
    report = args.report or (str(Path(args.sqlite).expanduser()) + ".postgres.json")
    from mac.sqlite_postgres_migration import migrate_sqlite_to_postgres

    def progress(message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    _print(
        migrate_sqlite_to_postgres(
            args.sqlite,
            dsn,
            report_path=report,
            batch_size=args.batch_size,
            resume=args.resume,
            restart=args.restart,
            verify_only=args.verify_only,
            progress=progress,
        )
    )


def cmd_task_summary(args: argparse.Namespace) -> None:
    """Glanceable per-task activity narrative: what the worker did, what the
    reviewer found/fixed, and any environment changes — a few lines per phase.
    Additive to the durable evidence/logs (see `task show` for those)."""
    detail = _plane(args).task_detail(args.task_id)
    data = detail.to_dict() if hasattr(detail, "to_dict") else detail
    task = (data.get("task") if isinstance(data, dict) else None) or data
    out: List[str] = []
    out.append("Task %s  [%s]" % (task.get("id", args.task_id), task.get("state", "?")))
    title = str(task.get("title") or "").strip()
    if title:
        out.append("  %s" % title)
    out.extend(_task_activity_lines(task, include_empty=True))
    sys.stdout.write("\n".join(out) + "\n")


def cmd_task_ready(args: argparse.Namespace) -> None:
    """List task-ready work with authoritative fleet eligibility attached."""
    cp = _plane(args)
    project = _effective_read_project(args)
    if hasattr(cp, "ready_task_explanations"):
        items = cp.ready_task_explanations(project=project, limit=args.limit or None)
    else:
        items = [
            cp.explain_task_dispatch(task.id)
            for task in cp.ready_tasks(project=project, limit=args.limit or None)
        ]
    rendered = []
    for item in items:
        data = item.to_dict() if hasattr(item, "to_dict") else dict(item)
        task = dict(data.pop("task", {}) or {})
        task["dispatch"] = data
        rendered.append(task)
    _print(rendered)


def cmd_task_why_unclaimed(args: argparse.Namespace) -> None:
    """Explain every task-level and agent-pair reason preventing dispatch."""
    _print(
        _plane(args).explain_task_dispatch(
            args.task_id,
            record_observation=True,
        )
    )


def cmd_task_claim(args: argparse.Namespace) -> None:
    """Claim a task for an agent."""
    cp = _plane(args)
    task, lease = cp.claim_task(args.task_id, args.agent_id)
    _print({"task": task.to_dict(), "lease_id": lease.id if lease else None})


def cmd_task_break_glass_authorize(args: argparse.Namespace) -> None:
    """Authorize one exact task/agent pair for direct host recovery execution."""

    _print(
        _plane(args).authorize_task_break_glass(
            args.task_id,
            args.agent_id,
            reason=args.reason,
            authorized_by=args.actor,
            ttl_seconds=args.ttl_seconds,
        )
    )


def cmd_task_break_glass_list(args: argparse.Namespace) -> None:
    """List break-glass authorizations for a task."""
    _print(
        _plane(args).list_task_break_glass_authorizations(
            task_id=args.task_id,
            limit=args.limit,
        )
    )


def cmd_task_break_glass_revoke(args: argparse.Namespace) -> None:
    """Revoke a task break-glass authorization."""
    _print(
        _plane(args).revoke_task_break_glass(
            args.authorization_id,
            revoked_by=args.actor,
            reason=args.reason,
        )
    )


def cmd_task_close(args: argparse.Namespace) -> None:
    """Close a task as succeeded or cancelled."""
    cp = _plane(args)
    from mac.models import TaskState, ValidationError

    if not args.success and not str(args.reason or "").strip():
        raise ValidationError("--reason is required with --cancelled")
    detail = {"reason": args.reason} if args.reason else {}
    target = TaskState.COMPLETED.value if args.success else TaskState.CANCELLED.value
    if not args.success:
        detail["disposition"] = args.disposition or "preserve"
        detail["cleanup_grace_seconds"] = int(
            max(0.0, float(args.cleanup_grace_days)) * 24 * 60 * 60
        )
        if args.replacement_task:
            detail["replacement_task_id"] = args.replacement_task
    result = cp.close_task(args.task_id, target, args.actor, detail)
    _maybe_emit_ticket(result, args, close_reason=args.reason or None)
    _print(result)


def cmd_task_ask(args: argparse.Namespace) -> None:
    """Park a task on an unanswered human question.

    This is deliberately NOT a failure. The work is still wanted; it just
    cannot proceed until a person answers. Tasks parked here are excluded
    from every sweeper and reaper, so they wait indefinitely rather than
    being retried into the ground or retired as bad work.
    """
    cp = _plane(args)
    questions = [{"question": q} for q in (args.question or []) if str(q or "").strip()]
    result = cp.request_task_input(
        args.task_id, questions, args.actor, why=args.why or ""
    )
    _print(result)


def cmd_task_needs_input(args: argparse.Namespace) -> None:
    """List tasks parked on an unanswered human question.

    This is the operator's inbox. Parked tasks are excluded from every
    sweeper and dispatch pass, so nothing surfaces them on its own -- without
    a way to list them they would wait forever, which is the failure this
    state was built to avoid, just slower.
    """
    from mac.models import TaskState

    cp = _plane(args)
    project = None if getattr(args, "all", False) else _effective_read_project(args)
    tasks = cp.list_tasks(
        TaskState.NEEDS_INPUT.value,
        project=project,
        limit=getattr(args, "limit", None) or None,
    )
    rows = []
    for task in tasks:
        record = task.to_dict() if hasattr(task, "to_dict") else dict(task)
        payload = (record.get("metadata") or {}).get("needs_input") or {}
        rows.append(
            {
                "id": record.get("id"),
                "title": record.get("title"),
                "project": record.get("project"),
                "asked_by": payload.get("asked_by"),
                "asked_at": payload.get("asked_at"),
                "questions": [
                    q.get("question") for q in (payload.get("questions") or [])
                ],
            }
        )
    _print(rows)


def _needs_input_buffer(record: Dict[str, Any]) -> str:
    """Render the $EDITOR buffer for a parked task."""
    payload = (record.get("metadata") or {}).get("needs_input") or {}
    lines = [
        "# Task %s" % record.get("id"),
        "# %s" % record.get("title"),
        "# State: %s" % record.get("state"),
        "#",
        "# Answer the question(s) below, then save and exit to submit.",
        "# Submitting returns this task to the pending queue.",
        "# Lines beginning with '#' are ignored. Save with no changes to abort.",
        "",
    ]
    questions = payload.get("questions") or []
    if questions:
        lines.append("# --- Asked by %s at %s ---" % (payload.get("asked_by") or "?", payload.get("asked_at") or "?"))
        for index, question in enumerate(questions, start=1):
            lines.append("# %d. %s" % (index, question.get("question") or ""))
            why = str(question.get("why") or "").strip()
            if why:
                lines.append("#    why: %s" % why)
        lines.append("")
    lines.append("## Answer")
    lines.append("")
    lines.append("")
    lines.append("## Description")
    lines.append(str(record.get("description") or ""))
    lines.append("")
    return "\n".join(lines)


def _parse_needs_input_buffer(text: str) -> Dict[str, str]:
    """Split an edited buffer back into its answer and description sections."""
    section = None
    parts: Dict[str, List[str]] = {"answer": [], "description": []}
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Answer"):
            section = "answer"
            continue
        if stripped.startswith("## Description"):
            section = "description"
            continue
        if stripped.startswith("#"):
            continue
        if section:
            parts[section].append(line)
    return {key: "\n".join(value).strip() for key, value in parts.items()}


def cmd_task_edit(args: argparse.Namespace) -> None:
    """Edit a parked task in $EDITOR; saving submits it back to the queue.

    Deliberately the same operation as the dashboard's Submit button: supply
    the missing information and the task returns to the pending queue. An
    unchanged buffer aborts, so opening a task to read it is not a write.
    """
    from mac.models import TaskState, ValidationError

    cp = _plane(args)
    task = cp.get_task(args.task_id)
    record = task.to_dict() if hasattr(task, "to_dict") else dict(task)
    if record.get("state") != TaskState.NEEDS_INPUT.value:
        raise ValidationError(
            "task %s is %s, not %s; only a task parked on a question can be "
            "answered by editing"
            % (record.get("id"), record.get("state"), TaskState.NEEDS_INPUT.value)
        )

    original = _needs_input_buffer(record)
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile(
        "w+", suffix=".mac-task.md", delete=False, encoding="utf-8"
    ) as handle:
        handle.write(original)
        path = handle.name
    try:
        subprocess.run("%s %s" % (editor, shlex.quote(path)), shell=True, check=True)
        edited = Path(path).read_text(encoding="utf-8")
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass

    if edited.strip() == original.strip():
        _print({"status": "unchanged", "id": record.get("id"), "submitted": False})
        return

    fields = _parse_needs_input_buffer(edited)
    before = _parse_needs_input_buffer(original)
    answer = fields["answer"]
    if not answer:
        raise ValidationError(
            "no answer supplied under '## Answer'; nothing was submitted"
        )
    if fields["description"] != before["description"]:
        cp.update_task(
            record["id"], description=fields["description"], actor=args.actor
        )
    _print(
        cp.answer_task_input(
            record["id"],
            answer,
            args.actor,
            disposition=getattr(args, "disposition", "resume"),
        )
    )


def cmd_task_answer(args: argparse.Namespace) -> None:
    """Record the answer to a parked question and dispose of the task.

    The disposition is required: answering is a judgement, and "no longer
    necessary" is as valid an answer as "yes, go ahead".
    """
    cp = _plane(args)
    result = cp.answer_task_input(
        args.task_id,
        args.answer,
        args.actor,
        disposition=args.disposition,
        replaced_by=args.replaced_by,
    )
    _print(result)


def _cancel_one_task(cp: Any, task_id: str, args: argparse.Namespace, reason: str) -> Any:
    """Cancel one task, including the terminal-state walk.

    Shared by the single-id and --selector paths deliberately: a bulk cancel
    that skipped the failed -> open -> cancelled step would silently refuse
    exactly the tasks an operator most wants to clean up.
    """
    from mac.models import TaskState

    detail = {
        "reason": reason,
        "disposition": args.disposition or "preserve",
        "cleanup_grace_seconds": int(
            max(0.0, float(args.cleanup_grace_days)) * 24 * 60 * 60
        ),
    }
    if getattr(args, "replacement_task", None):
        detail["replacement_task_id"] = args.replacement_task

    current = str(_task_state(cp, task_id) or "")
    if current == TaskState.CANCELLED.value:
        return cp.get_task(task_id)
    if current == TaskState.COMPLETED.value:
        raise MACError(
            "task %s is completed; cancelling would discard a finished result"
            % task_id
        )
    if current == TaskState.FAILED.value:
        cp.reopen_task(task_id, args.actor, "reopened only to cancel: %s" % reason)
    return cp.close_task(task_id, TaskState.CANCELLED.value, args.actor, detail)


def cmd_task_cancel(args: argparse.Namespace) -> None:
    """Actively cancel a task and revoke any live worker assignment.

    The hub transition clears the task's lease immediately. Loop workers poll
    assignment authority while an executor is running and terminate the active
    subprocess tree when that lease is no longer current.
    """

    cp = _plane(args)
    from mac.models import TaskState

    reason = str(args.reason or "").strip() or "operator requested cancellation"

    # Bulk path. The operator asked for this explicitly -- cleaning thousands
    # of bad tasks one id at a time is not a workflow -- and it is dry by
    # default for exactly the same reason it is useful.
    if getattr(args, "selector", None):
        if args.task_id:
            raise SystemExit(
                "give a task id or --selector, not both: one names a task, the "
                "other names a group, and mixing them hides which one ran"
            )
        # Fetch UNSCOPED and let the selector do all the narrowing. Scoping
        # the fetch to the cwd project while the selector names another one
        # silently finds nothing -- which on a bulk cleanup reads as "there is
        # nothing to clean" rather than "you asked the wrong question". The
        # selector is the group; nothing else may quietly shrink it.
        candidates = [
            task.to_dict() if hasattr(task, "to_dict") else dict(task)
            for task in cp.list_tasks(None, project=None)
        ]
        selected = _selector_guarded_mutation(candidates, args, "task", "cancelled")
        if selected is None:
            return
        results = []
        for record in selected:
            try:
                results.append(_cancel_one_task(cp, record["id"], args, reason))
            except Exception as exc:  # noqa: BLE001 - one refusal must not stop the sweep
                print(
                    "  %s: %s" % (record["id"], exc),
                    file=sys.stderr,
                )
        print("cancelled %d of %d" % (len(results), len(selected)), file=sys.stderr)
        _print(results)
        return
    detail = {
        "reason": reason,
        "disposition": args.disposition or "preserve",
        "cleanup_grace_seconds": int(
            max(0.0, float(args.cleanup_grace_days)) * 24 * 60 * 60
        ),
    }
    if args.replacement_task:
        detail["replacement_task_id"] = args.replacement_task

    # Cancelling means "make this task stop and go away", and that intent does
    # not change because the task already failed. The state machine only allows
    # failed -> open, so cancelling a failed task used to return a 400 and hand
    # the operator a two-step dance (`reopen`, then `cancel`) to express one
    # decision they had already made.
    #
    # Deliberately done HERE and not in the service: failed -> cancelled stays
    # an illegal transition, because the state machine is an invariant other
    # code reasons about. This is the human-facing command taking the two legal
    # steps on the operator's behalf, and recording that it did so -- the
    # reopen reason says it is part of a cancellation, so the history cannot be
    # misread as someone intending a retry.
    current = str(_task_state(cp, args.task_id) or "")
    if current == TaskState.CANCELLED.value:
        # Already where the operator wants it. Saying so beats a 400.
        print("task %s is already cancelled" % args.task_id, file=sys.stderr)
        _print(cp.get_task(args.task_id))
        return
    if current == TaskState.COMPLETED.value:
        # The one terminal state that must NOT be walked back: completed work
        # is a real outcome with evidence and a publication behind it, and
        # quietly erasing it is not what anyone means by "cancel".
        raise SystemExit(
            "task %s is completed; cancelling would discard a finished result. "
            "Use `mac task reopen %s` if it genuinely needs more work."
            % (args.task_id, args.task_id)
        )
    if current == TaskState.FAILED.value:
        cp.reopen_task(
            args.task_id,
            args.actor,
            "reopened only to cancel: %s" % reason,
        )

    result = cp.close_task(
        args.task_id,
        TaskState.CANCELLED.value,
        args.actor,
        detail,
    )
    _maybe_emit_ticket(result, args, close_reason=reason)
    _print(result)


def _task_state(cp: Any, task_id: str) -> Optional[str]:
    """Current state of a task, or None if it cannot be read.

    None deliberately means "carry on and let the hub decide": a read failure
    must not turn a cancel into a refusal, since the transition itself is the
    authority.
    """
    try:
        task = cp.get_task(task_id)
    except Exception:  # noqa: BLE001 - the transition below is the real gate
        return None
    record = task.to_dict() if hasattr(task, "to_dict") else task
    if isinstance(record, dict):
        return record.get("state")
    return getattr(task, "state", None)


def cmd_task_reopen(args: argparse.Namespace) -> None:
    # Recovery: return a stuck/terminal task to OPEN (failed/cancelled reset
    # attempt_count so the requeue isn't immediately re-exhausted).
    """Reopen a stuck or terminal task back to OPEN."""
    _print(_plane(args).reopen_task(args.task_id, args.actor, args.reason or None))


def cmd_task_recover_stranded(args: argparse.Namespace) -> None:
    """Re-supervise tasks left waiting on a dependency that will never finish.

    Dry-run by default: this mutates live task state, so the default reports
    what it would touch and --apply is required to actually do it.
    """
    _print(
        _plane(args).recover_stranded_dependents(
            limit=args.limit,
            dry_run=not args.apply,
        )
    )


def cmd_task_recover_finalizer(args: argparse.Namespace) -> None:
    """Explicitly recover preserved work refused only for uncommitted new files."""

    from mac.repository_recovery import (
        RepositoryRecoveryError,
        recover_finalizer_worktree,
    )

    try:
        result = recover_finalizer_worktree(
            args.workspace,
            approved_new_files=args.approve_new_file or [],
            original_evidence_id=args.evidence_id or "",
            execute=bool(args.execute),
        )
    except RepositoryRecoveryError as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_task_recover_stalled_finalizer(args: argparse.Namespace) -> None:
    """Resume a deterministic finalizer that stalled after harvesting verified work."""

    from mac.repository_recovery import (
        RepositoryRecoveryError,
        recover_stalled_finalizer,
    )

    try:
        result = recover_stalled_finalizer(
            args.workspace,
            approved_new_files=args.approve_new_file or [],
            original_evidence_id=args.evidence_id or "",
            execute=bool(args.execute),
        )
    except RepositoryRecoveryError as exc:
        raise MACError(str(exc)) from exc
    _print(result)


def cmd_task_force_complete(args: argparse.Namespace) -> None:
    # Operator override: mark a task COMPLETED regardless of state/review, for
    # reconciling work done out-of-band or recovering a stranded terminal task.
    """Force a task to COMPLETED regardless of its state."""
    _print(_plane(args).force_complete_task(args.task_id, args.actor, args.reason or None))


def _repository_open_pull_requests(repo: Path) -> tuple[Optional[Dict[str, str]], str]:
    return query_open_pull_requests(repo, runner=subprocess.run)


def _repository_ref_audit(
    args: argparse.Namespace,
) -> tuple[Any, list[RepositoryRefAudit], str, list[str]]:
    repo = Path(args.repo_path).expanduser().resolve()
    if not repo.is_dir():
        raise RepositoryHygieneError("repository path does not exist")
    cp = _plane(args)
    refs = list_managed_remote_refs(repo, args.remote)
    selected_tasks = set(args.task_ids or [])
    if selected_tasks:
        refs = [item for item in refs if item.task_id in selected_tasks]
    open_prs, pr_warning = _repository_open_pull_requests(repo)
    result = audit_repository_refs_result(
        repo,
        refs,
        cp.task_detail,
        base_ref=args.base_ref or ("%s/main" % args.remote),
        default_grace_seconds=int(max(0.0, float(args.grace_days)) * 24 * 60 * 60),
        open_pull_requests=open_prs,
    )
    warning = "; ".join(part for part in (pr_warning, result.warning) if part)
    return cp, result.audits, warning, result.timed_out_task_ids


def _repository_ref_report(
    audits: Iterable[RepositoryRefAudit],
    *,
    pr_warning: str = "",
    timed_out_task_ids: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    items = [item.to_dict() for item in audits]
    counts: Dict[str, int] = {}
    for item in items:
        classification = str(item.get("classification") or "unknown")
        counts[classification] = counts.get(classification, 0) + 1
    report: Dict[str, Any] = {
        "schema": REPOSITORY_REF_CLEANUP_SCHEMA,
        "mode": "audit",
        "counts": counts,
        "eligible_count": sum(1 for item in items if item.get("eligible")),
        "refs": items,
    }
    if pr_warning:
        report["warning"] = pr_warning
    timed_out = [str(tid) for tid in (timed_out_task_ids or []) if str(tid)]
    if timed_out:
        report["timed_out_task_ids"] = timed_out
    return report


def cmd_repo_refs_audit(args: argparse.Namespace) -> None:
    """Audit repository refs and report their status."""
    _cp, audits, warning, timed_out = _repository_ref_audit(args)
    _print(
        _repository_ref_report(
            audits, pr_warning=warning, timed_out_task_ids=timed_out
        )
    )


def cmd_repo_refs_prune(args: argparse.Namespace) -> None:
    """Prune stale repository refs."""
    cp, audits, warning, timed_out = _repository_ref_audit(args)
    if args.execute and warning:
        raise RepositoryHygieneError(
            "%s; refusing executable cleanup" % warning
        )

    def record(item: RepositoryRefAudit, action: str, error: str) -> None:
        metadata = cleanup_evidence_metadata(item, action, error=error)
        cp.add_evidence(
            item.task_id,
            "artifact",
            "urn:mac:repository-ref-cleanup:%s:%s:%s:%s"
            % (item.task_id, item.lease_id, item.sha, action),
            "managed repository ref cleanup %s for %s at %s"
            % (action, item.branch, item.sha),
            args.actor,
            metadata=metadata,
            _trusted_internal=True,
        )

    result = prune_repository_refs(
        Path(args.repo_path),
        audits,
        execute=bool(args.execute),
        recorder=record if args.execute else None,
    )
    result["audit"] = _repository_ref_report(
        audits, pr_warning=warning, timed_out_task_ids=timed_out
    )
    _print(result)


def cmd_repo_refs_status(args: argparse.Namespace) -> None:
    """Show the repository ref reconciler status."""
    _print(_plane(args).repository_ref_reconciler_status())


def cmd_repo_refs_reconcile(args: argparse.Namespace) -> None:
    """Reconcile repository refs."""
    _print(
        _plane(args).reconcile_repository_refs(
            mode=args.mode,
            actor=args.actor,
        )
    )


def _selector_options(args: argparse.Namespace) -> Dict[str, Any]:
    """Collect the per-operation options from parsed flags."""
    options: Dict[str, Any] = {}
    for name in (
        "answer",
        "reason",
        "disposition",
        "replacement_task",
        "target_state",
        "title",
        "description",
        "project",
        "priority",
        "max_attempts",
    ):
        value = getattr(args, name, None)
        if value is not None:
            options[name] = value
    capabilities = getattr(args, "required_capabilities", None)
    if capabilities:
        options["required_capabilities"] = [
            item.strip() for item in capabilities.split(",") if item.strip()
        ]
    for name in ("metadata_merge", "metadata_replace", "metadata_set"):
        raw = getattr(args, name, None)
        if raw:
            options[name] = json.loads(raw)
    unset = getattr(args, "metadata_unset", None)
    if unset:
        options["metadata_unset"] = [
            path.strip() for path in unset.split(",") if path.strip()
        ]
    return options


def cmd_task_select(args: argparse.Namespace) -> None:
    """Preview the group a selector names, changing nothing.

    This is the command an operator runs first and reruns after every
    refinement, so it prints the count and a sample rather than the whole
    group -- the count is the number a bulk operation is about to act on.
    """
    cp = _plane(args)
    result = cp.select_tasks(args.selector, limit=args.limit, sample=args.sample)
    _print(result)


def cmd_task_batch(args: argparse.Namespace) -> None:
    """Apply one operation to every task a selector names.

    Dry by default. Without --apply this reports exactly what would change and
    writes nothing, which is the only safe default for a command whose blast
    radius is an expression.
    """
    cp = _plane(args)
    result = cp.apply_task_batch(
        args.selector,
        args.operation,
        actor=args.actor,
        apply=args.apply,
        expect_count=args.expect_count,
        expect_token=args.expect_token,
        limit=args.limit,
        **_selector_options(args),
    )
    _print(result)


def cmd_task_group_save(args: argparse.Namespace) -> None:
    cp = _plane(args)
    _print(
        cp.save_task_group(
            args.name,
            args.selector,
            description=args.description or "",
            actor=args.actor,
        )
    )


def cmd_task_group_list(args: argparse.Namespace) -> None:
    _print(_plane(args).list_task_groups())


def cmd_task_group_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_task_group(args.name))


def cmd_task_group_delete(args: argparse.Namespace) -> None:
    _print(_plane(args).delete_task_group(args.name))


def cmd_task_search(args: argparse.Namespace) -> None:
    """Search tasks matching a query."""
    cp = _plane(args)
    project = _effective_read_project(args)
    _print([t.to_dict() for t in cp.search_tasks(args.query, project=project, limit=int(args.limit))])


def cmd_diagnostics(args: argparse.Namespace) -> None:
    """Print control-plane diagnostics.

    The report is produced by ``diagnostics_report`` on the resolved transport:
    against the in-process ControlPlane for ``--db`` / local authority, or over
    the hub API for a remote client. Either way the checks execute against the
    authoritative backend — never a direct-SQL fallback or an accidental local
    database.
    """
    cp = _plane(args)
    names = getattr(args, "check", None) or None
    report = _unwrap(cp.diagnostics_report(names=names))
    _print(report)


def cmd_task_stats(args: argparse.Namespace) -> None:
    """Print task statistics for the effective project."""
    cp = _plane(args)
    project = _effective_read_project(args)
    _print(cp.task_stats(project=project))


def cmd_task_generator_yield(args: argparse.Namespace) -> None:
    """Print each task origin's filed/completed record and gate standing."""
    _print(_plane(args).generator_yield_report())


def cmd_task_throughput(args: argparse.Namespace) -> None:
    """Print task-flow KPIs, stranded work, and shared-resource collisions."""

    project = _effective_read_project(args)
    _print(
        _plane(args).task_flow_report(
            project=project,
            since_hours=float(args.since_hours),
            warning_seconds=float(args.warning_minutes) * 60.0,
            critical_seconds=float(args.critical_minutes) * 60.0,
            refresh_limit=int(args.refresh_limit),
        )
    )


def cmd_task_audit(args: argparse.Namespace) -> None:
    """Audit the complete ledger without applying any state transitions."""

    report = _plane(args).task_ledger_audit(
        project=args.project,
        verify_git=not bool(args.no_git),
        offset=args.offset,
        limit=args.limit,
    )
    report = _unwrap(report)
    if _OUTPUT_JSON:
        _print(report)
        return
    snapshot = report.get("snapshot") or {}
    summary = report.get("summary") or {}
    verdicts = summary.get("verdict_counts") or {}
    print(
        "Audited %s task(s) across every project; snapshot changed during run: %s"
        % (snapshot.get("task_count", 0), "yes" if snapshot.get("changed_during_run") else "no")
    )
    print(
        "  verified=%s  active=%s  needs-review=%s  contradictions=%s  unresolved=%s"
        % (
            verdicts.get("verified", 0),
            verdicts.get("active_valid", 0),
            verdicts.get("needs_review", 0),
            verdicts.get("contradiction", 0),
            summary.get("unresolved_count", 0),
        )
    )
    print("  task-set %s" % (snapshot.get("task_set_digest") or "unknown"))
    unresolved = [
        row
        for row in report.get("tasks") or []
        if ((row.get("assessment") or {}).get("verdict"))
        in {"contradiction", "needs_review"}
    ]
    if unresolved:
        print("\nUnresolved:")
        for row in unresolved:
            assessment = row.get("assessment") or {}
            findings = ",".join(assessment.get("findings") or [])
            print(
                "  %-13s %-11s %-13s %-10s %s%s"
                % (
                    _short_task_id(str(row.get("task_id") or "")),
                    row.get("state") or "?",
                    assessment.get("verdict") or "?",
                    row.get("project") or "-",
                    _trunc(row.get("title") or "", 58),
                    ("  [" + findings + "]") if findings else "",
                )
            )


def cmd_project_list(args: argparse.Namespace) -> None:
    """List registered projects."""
    _print(_apply_selector(list(_plane(args).list_projects()), args, "project"))


def cmd_project_create(args: argparse.Namespace) -> None:
    # New projects default to dispatch-PAUSED so a freshly-onboarded backlog
    # does not auto-claim before an operator activates the project. Pass
    # --active to opt straight into autonomous dispatch.
    """Create a project, dispatch-paused by default."""
    _print(
        _plane(args).create_project(
            args.name,
            description=args.description or "",
            metadata=_json_arg(args.metadata, {}),
            status=args.status,
            actor=args.actor,
            project_id=args.project_id,
            dispatch_paused=args.dispatch_paused,
        )
    )


_PROJECT_REGISTER_REMOTE_PREFIXES = ("https://", "git@", "ssh://", "git://")


def _project_register_git(
    checkout: Path,
    *git_args: str,
    required: bool = True,
) -> Optional[str]:
    git = shutil.which("git")
    if git is None:
        raise MACError("git is required to register a local checkout")
    try:
        result = subprocess.run(
            [git, "-C", str(checkout), *git_args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise MACError(
            "timed out while inspecting Git checkout %s" % checkout
        ) from exc
    except OSError as exc:
        raise MACError(
            "failed to inspect Git checkout %s: %s" % (checkout, exc)
        ) from exc
    value = result.stdout.strip()
    if result.returncode == 0 and value:
        return value
    if not required:
        return None
    detail = result.stderr.strip()
    suffix = ": %s" % detail if detail else ""
    raise MACError(
        "cannot inspect Git checkout %s with `git %s`%s"
        % (checkout, " ".join(git_args), suffix)
    )


def _local_project_registration(
    target: str,
    *,
    default_branch: Optional[str],
) -> tuple[str, str, Path]:
    checkout = Path(target or ".").expanduser()
    if not checkout.exists():
        raise MACError("project checkout path does not exist: %s" % checkout)
    if not checkout.is_dir():
        raise MACError("project checkout path is not a directory: %s" % checkout)
    checkout = checkout.resolve()
    root_value = _project_register_git(checkout, "rev-parse", "--show-toplevel")
    assert root_value is not None
    root = Path(root_value).resolve()

    repository_url = _project_register_git(root, "remote", "get-url", "origin")
    assert repository_url is not None
    from mac.repository_contract import canonical_git_remote_identity

    try:
        canonical_git_remote_identity(repository_url)
    except ValueError as exc:
        raise MACError(
            "checkout origin is not a supported secret-free Git remote: %s" % exc
        ) from exc

    branch = str(default_branch or "").strip()
    if not branch:
        origin_head = _project_register_git(
            root,
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/origin/HEAD",
            required=False,
        )
        if origin_head and origin_head.startswith("origin/"):
            branch = origin_head[len("origin/") :]
        if not branch:
            branch = (
                _project_register_git(
                    root,
                    "symbolic-ref",
                    "--quiet",
                    "--short",
                    "HEAD",
                    required=False,
                )
                or ""
            )
    if not branch:
        raise MACError(
            "cannot infer a branch from checkout %s; pass --branch BRANCH" % root
        )
    return repository_url, branch, root


def _resolve_project_registration_target(
    target: Optional[str],
    *,
    default_branch: Optional[str],
) -> tuple[str, Optional[str], Optional[Path]]:
    value = str(target or "").strip()
    if value.startswith(_PROJECT_REGISTER_REMOTE_PREFIXES):
        return value, default_branch, None
    repository_url, branch, root = _local_project_registration(
        value or ".",
        default_branch=default_branch,
    )
    return repository_url, branch, root


def cmd_project_register(args: argparse.Namespace) -> None:
    """Register an existing repository as a project."""
    repository_url, default_branch, checkout = _resolve_project_registration_target(
        args.repository_url,
        default_branch=args.default_branch,
    )
    if checkout is not None:
        print(
            "mac: registering checkout %s (branch %s)"
            % (checkout, default_branch),
            file=sys.stderr,
        )
    capabilities = list(_csv(args.required_capabilities)) or None
    _print(
        _plane(args).register_project(
            repository_url,
            project=args.project,
            default_branch=default_branch,
            title=args.title,
            priority=args.priority,
            required_capabilities=capabilities,
            actor=args.actor,
        )
    )


def cmd_project_update(args: argparse.Namespace) -> None:
    """Update a project's metadata."""
    if not any(
        value is not None
        for value in (
            args.name,
            args.description,
            args.metadata,
            args.status,
            args.repository_registration,
            args.default_branch,
        )
    ):
        raise MACError("project update requires at least one changed field")
    _print(
        _plane(args).update_project(
            args.project,
            name=args.name,
            description=args.description,
            metadata=(
                _json_arg(args.metadata, {})
                if args.metadata is not None
                else None
            ),
            status=args.status,
            repository_registration=args.repository_registration,
            default_branch=args.default_branch,
            actor=args.actor,
        )
    )


def cmd_project_unregister(args: argparse.Namespace) -> None:
    """Unregister and delete a project."""
    result = _plane(args).delete_project(
        args.project,
        force=args.force,
        actor=args.actor,
    )
    _print(result or {"deleted": args.project})


def cmd_project_pause(args: argparse.Namespace) -> None:
    """Pause dispatch for a project."""
    _print(_plane(args).set_project_dispatch(args.project, paused=True, actor=args.actor))


def cmd_project_activate(args: argparse.Namespace) -> None:
    """Activate dispatch for a project."""
    _print(_plane(args).set_project_dispatch(args.project, paused=False, actor=args.actor))


def cmd_project_show(args: argparse.Namespace) -> None:
    """Show details for a project."""
    _print(_plane(args).get_project(args.project))


def _derive_sandbox_bom(args: argparse.Namespace) -> Dict[str, Any]:
    """Derive the BOM from EVERY repository registration's contract.

    Registrations, not projects, for three reasons that all bit a first
    implementation of this:

    * A contract belongs to a repository, so a project with several repos has
      several contracts, and the registration is where they live.
    * ``project list`` does not show every registration. Two OrcaSlicer repos
      are registered and neither appears there; deriving per project silently
      omitted both.
    * A branch-qualified project name contains a slash
      (``isaacsim7-poc@feat/ros-sim``), and ``GET /projects/{project}`` 404s on
      it however the name is escaped. That project's contract requires cmake and
      ninja, so the per-project derivation dropped a real toolchain on the floor.

    One list call has none of those failure modes.
    """
    return sandbox_bom.derive_bom(_plane(args).list_project_repositories())


def cmd_sandbox_bom(args: argparse.Namespace) -> None:
    """Derive the sandbox bill of materials from every project's contract."""
    derived = _derive_sandbox_bom(args)

    if args.containerfile:
        text = Path(args.containerfile).read_text(encoding="utf-8")
        derived = dict(derived, gaps=sandbox_bom.bom_gaps(derived, text))

    if args.compare:
        committed = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        drift = sandbox_bom.manifest_drift(committed, derived)
        _print({"drift": drift, "has_drift": sandbox_bom.manifest_has_drift(drift)})
        if sandbox_bom.manifest_has_drift(drift):
            raise SystemExit(1)
        return

    if args.write:
        target = Path(args.write)
        target.write_text(
            json.dumps(sandbox_bom.manifest(derived), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        # Writing the manifest is NOT deploying it. The image is rebuilt and
        # republished by the reviewed workflow, and the frozen-input hash
        # changes when this file does -- which is the point.
        _print({"written": str(target), "packages": derived.get("packages")})
        return

    _print(derived)


def cmd_sandbox_rollout(args: argparse.Namespace) -> None:
    """File one drained-worker barrier task per agent for a reviewed image."""
    cp = _plane(args)
    bom = {}
    if args.manifest:
        bom = json.loads(Path(args.manifest).read_text(encoding="utf-8"))
    _print(
        cp.roll_out_sandbox_image(
            args.image, bom=bom, actor=args.actor, project=args.project
        )
    )


def cmd_work_package_update(args: argparse.Namespace) -> None:
    """Change a work package's goal or metadata."""
    _print(
        _plane(args).update_work_package(
            args.package_id,
            goal=args.goal,
            metadata=_json_arg(args.metadata, None),
            actor=args.actor,
        )
    )


def cmd_work_package_cancel(args: argparse.Namespace) -> None:
    """Terminally abandon a work package."""
    _print(
        _plane(args).cancel_work_package(
            args.package_id, actor=args.actor, reason=args.reason
        )
    )


def cmd_work_package_list(args: argparse.Namespace) -> None:
    """List work packages."""
    _print(
        _apply_selector(
            list(
                _plane(args).list_work_packages(
                    state=args.state,
                    project=args.project,
                    limit=args.limit,
                )
            ),
            args,
            "work-package",
        )
    )


def cmd_work_package_admit(args: argparse.Namespace) -> None:
    """Admit a work package with an optional plan."""
    plan = _read_json_arg(
        args.plan,
        args.plan_file,
        label="work-package plan",
        default=None,
    )
    if not isinstance(plan, dict):
        raise SystemExit("work-package plan must be a JSON object")
    _print(
        _plane(args).admit_work_package(
            plan,
            actor=args.actor,
            reason=args.reason,
            tenant_id=args.tenant_id,
            root_task_id=args.root_task_id,
        )
    )


def cmd_work_package_show(args: argparse.Namespace) -> None:
    """Show details for a work package."""
    _print(_plane(args).describe_work_package(args.package_id))


def cmd_work_package_readiness(args: argparse.Namespace) -> None:
    """Show activation readiness for a work package."""
    _print(_plane(args).work_package_activation_readiness(args.package_id))


def cmd_work_package_activate(args: argparse.Namespace) -> None:
    """Activate a work package."""
    _print(
        _plane(args).activate_work_package(
            args.package_id,
            expected_plan_version=args.plan_version,
            expected_epoch=args.epoch,
            actor=args.actor,
        )
    )


def _work_package_replan_plan(args: argparse.Namespace) -> Dict[str, Any]:
    plan = _read_json_arg(
        args.plan,
        args.plan_file,
        label="work-package replan",
        default=None,
    )
    if not isinstance(plan, dict):
        raise SystemExit("work-package replan must be a JSON object")
    return plan


def cmd_work_package_replan_preview(args: argparse.Namespace) -> None:
    """Preview a work-package replan."""
    _print(
        _plane(args).preview_work_package_replan(
            args.package_id,
            _work_package_replan_plan(args),
            expected_plan_version=args.plan_version,
            expected_epoch=args.epoch,
            actor=args.actor,
            reason=args.reason,
        )
    )


def cmd_work_package_pause(args: argparse.Namespace) -> None:
    """Pause a work package."""
    _print(
        _plane(args).pause_work_package(
            args.package_id,
            expected_plan_version=args.plan_version,
            expected_epoch=args.epoch,
            actor=args.actor,
            reason=args.reason,
        )
    )


def cmd_work_package_replan(args: argparse.Namespace) -> None:
    """Replan a work package."""
    _print(
        _plane(args).replan_work_package(
            args.package_id,
            _work_package_replan_plan(args),
            expected_plan_version=args.plan_version,
            expected_epoch=args.epoch,
            actor=args.actor,
            reason=args.reason,
        )
    )


def cmd_work_package_verify_output(args: argparse.Namespace) -> None:
    """Verify a work-package output evidence record."""
    _print(
        _plane(args).verify_work_package_output(
            args.evidence_id,
            actor=args.actor,
        )
    )


def cmd_work_package_accept_candidate(args: argparse.Namespace) -> None:
    """Accept a work-package candidate."""
    _print(
        _plane(args).accept_work_package_candidate(
            args.candidate_id,
            actor=args.actor,
        )
    )


def cmd_work_package_reject_candidate(args: argparse.Namespace) -> None:
    """Reject a work-package candidate."""
    _print(
        _plane(args).reject_work_package_candidate(
            args.candidate_id,
            actor=args.actor,
            reason=args.reason,
        )
    )


def cmd_work_package_assemble(args: argparse.Namespace) -> None:
    """Assemble a work package at an integration node."""
    _print(
        _plane(args).assemble_work_package(
            args.package_id,
            args.integration_node_key,
            actor=args.actor,
        )
    )


def cmd_work_package_assembly_status(args: argparse.Namespace) -> None:
    """Show work-package integration batch status."""
    _print(_plane(args).work_package_integration_status(args.batch_id))


def cmd_work_package_assembly_claim(args: argparse.Namespace) -> None:
    """Claim a work-package integration batch."""
    _print(_plane(args).claim_work_package_integration_batch(args.batch_id))


def cmd_work_package_assemble_batch(args: argparse.Namespace) -> None:
    """Assemble a work-package integration batch."""
    _print(_plane(args).assemble_work_package_integration_batch(args.batch_id))


def cmd_work_package_certification_prepare(args: argparse.Namespace) -> None:
    """Prepare a work-package certification job."""
    _print(
        _plane(args).prepare_work_package_certification_job(
            args.batch_id,
            args.bundle_path,
            actor=args.actor,
        )
    )


def cmd_work_package_certification_status(args: argparse.Namespace) -> None:
    """Show a work-package certification job status."""
    _print(_plane(args).work_package_certification_status(args.job_id))


def cmd_work_package_certification_claim(args: argparse.Namespace) -> None:
    """Claim a work-package certification job."""
    _print(
        _plane(args).claim_work_package_certification_job(
            args.job_id,
            owner=args.owner,
        )
    )


def cmd_work_package_certification_ingest(args: argparse.Namespace) -> None:
    """Ingest a work-package certification result."""
    result = _read_json_arg(
        args.result,
        args.result_file,
        label="work-package certification result",
        default=None,
    )
    if not isinstance(result, dict):
        raise SystemExit("work-package certification result must be a JSON object")
    _print(
        _plane(args).ingest_work_package_certification_result(
            args.job_id,
            result,
            owner=args.owner,
            fence=args.fence,
        )
    )


def cmd_work_package_certification_run(args: argparse.Namespace) -> None:
    """Run a work-package certification job."""
    _print(
        _plane(args).run_work_package_certification_job(
            args.job_id,
            args.bundle_path,
            owner=args.owner,
            result_path=args.result_path,
        )
    )


def cmd_work_package_reject_failed_certification(args: argparse.Namespace) -> None:
    """Reject a failed work-package certification."""
    _print(
        _plane(args).reject_failed_work_package_certification(
            args.batch_id,
            args.certification_id,
            actor=args.actor,
        )
    )


def cmd_work_package_accept_certification(args: argparse.Namespace) -> None:
    """Accept a work-package certification."""
    _print(
        _plane(args).accept_work_package_certification(
            args.batch_id,
            args.certification_id,
        )
    )


def cmd_work_package_land(args: argparse.Namespace) -> None:
    """Land a work-package integration batch."""
    _print(_plane(args).land_work_package(args.batch_id))


def cmd_work_package_finalize_publication(args: argparse.Namespace) -> None:
    """Finalize publication of a work-package batch."""
    _print(
        _plane(args).finalize_work_package_publication(
            args.batch_id,
            actor=args.actor,
            receipt_id=args.receipt_id,
        )
    )


def cmd_task_start(args: argparse.Namespace) -> None:
    """Start a claimed task for an agent."""
    _print(
        _plane(args).start_task(
            args.task_id,
            args.agent_id,
            lease_id=args.lease_id,
        )
    )


def cmd_task_release(args: argparse.Namespace) -> None:
    """Release a task back to the pool."""
    _print(_plane(args).release_task(args.task_id, actor=args.actor))


def cmd_task_submit(args: argparse.Namespace) -> None:
    """Submit a task for review."""
    _print(
        _plane(args).submit_for_review(
            args.task_id,
            args.agent_id,
            lease_id=args.lease_id,
        )
    )


def cmd_task_evidence(args: argparse.Namespace) -> None:
    # Validate against the same single source of truth the runtime uses so an
    # unsupported --kind fails fast at the CLI with a consistent message instead
    # of only after a round trip to the control plane.
    """Attach evidence to a task."""
    try:
        kind = normalize_evidence_kind(args.kind)
    except ValueError as exc:
        raise MACError(str(exc)) from exc
    _print(
        _plane(args).add_evidence(
            args.task_id,
            kind,
            args.uri,
            args.summary,
            args.created_by,
            checksum=args.checksum,
            lease_id=args.lease_id,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_machine_register(args: argparse.Namespace) -> None:
    """Register a machine."""
    _print(
        _plane(args).register_machine(
            hostname=args.hostname,
            labels=_json_arg(args.labels, {}),
            resources=_json_arg(args.resources, {}),
            trusted=not args.untrusted,
            machine_id=args.machine_id,
        )
    )


def _machine_hw_summary(d: dict) -> str:
    """Compact hardware string for a machine line, reusing _agent_hw_summary
    style.  Machine records carry hardware in two places: the top-level
    ``hardware`` column (mirrored from agent heartbeats) and inside
    ``resources.hardware`` (set at registration time).  Prefer the top-level
    ``hardware`` field when populated; fall back to ``resources.hardware`` so
    machines registered with explicit hardware resources also show a summary."""
    hw = d.get("hardware") or {}
    if not hw:
        res = d.get("resources") or {}
        hw = (res.get("hardware") or {}) if isinstance(res, dict) else {}
    return _agent_hw_summary({"resources": {"hardware": hw}})


def cmd_machine_list(args: argparse.Namespace) -> None:
    """List registered machines."""
    machines = _plane(args).list_machines()
    if _OUTPUT_JSON:
        _print([m.to_dict() if hasattr(m, "to_dict") else m for m in machines])
        return
    for m in machines:
        d = m.to_dict() if hasattr(m, "to_dict") else m
        trusted_flag = "trusted" if d.get("trusted") else "untrusted"
        last_seen = str(d.get("last_seen_at") or "-")[:19]
        hw = _machine_hw_summary(d)
        print("%-36s  %-36s  %-9s  %-19s  %s" % (
            d.get("id", ""),
            d.get("hostname", ""),
            trusted_flag,
            last_seen,
            hw,
        ))


def cmd_machine_show(args: argparse.Namespace) -> None:
    """Show details for a machine."""
    _print(_plane(args).get_machine(args.machine_id))


def cmd_agent_register(args: argparse.Namespace) -> None:
    """Register an agent on a machine."""
    _print(
        _plane(args).register_agent(
            machine_id=args.machine_id,
            name=args.name,
            capabilities=_csv(args.capabilities),
            resources=_json_arg(args.resources, {}),
            agent_id=args.agent_id,
            persona_instance_id=args.persona_instance_id,
            instance_kind=getattr(args, "instance_kind", None),
        )
    )


def cmd_agent_update(args: argparse.Namespace) -> None:
    """Change an agent's instance kind or its advertised capabilities.

    ControlPlane.update_agent and PUT /agents/{id} have always accepted
    `capabilities`; only this command ignored it, so the sole way to change what
    an agent advertises was to re-register it through a fleet deploy. That
    matters more than it sounds: capabilities are DECLARED, not probed, and the
    default worker list omits `c`, `make` and `python3` entirely. On 2026-08-08
    every fleet host had the full toolchain installed and exactly one agent
    advertised `c`, which is why a single transient failure could make C work
    permanently undispatchable.

    --add/--remove edit the set in place, which is what you want when the
    machine gained a toolchain and the record has not caught up. --capabilities
    replaces it wholesale.
    """
    cp = _plane(args)
    capabilities: Optional[List[str]] = None
    if args.capabilities is not None:
        capabilities = [c.strip() for c in args.capabilities.split(",") if c.strip()]
    if args.add_capability or args.remove_capability:
        if capabilities is None:
            record = cp.get_agent(args.agent_id)
            current = record.to_dict() if hasattr(record, "to_dict") else dict(record)
            capabilities = list(current.get("capabilities") or [])
        for value in args.add_capability or []:
            if value not in capabilities:
                capabilities.append(value)
        for value in args.remove_capability or []:
            if value in capabilities:
                capabilities.remove(value)
    updates: Dict[str, Any] = {}
    if args.instance_kind is not None:
        updates["instance_kind"] = args.instance_kind
    if capabilities is not None:
        updates["capabilities"] = sorted(set(capabilities))
    if not updates:
        raise SystemExit(
            "nothing to update: supply --instance-kind, --capabilities, "
            "--add-capability or --remove-capability"
        )
    _print(cp.update_agent(args.agent_id, **updates))


def _egress_contract_block(task: Any) -> Dict[str, Any]:
    """The task's TOP-LEVEL metadata.egress_contract, as a plain dict."""
    record = task.to_dict() if hasattr(task, "to_dict") else task
    metadata = (record or {}).get("metadata") if isinstance(record, dict) else None
    block = (metadata or {}).get("egress_contract") if isinstance(metadata, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def _write_egress_hosts(
    cp: Any, task_id: str, hosts: List[str], args: argparse.Namespace, reason: str
) -> Any:
    """Persist the host list at metadata.egress_contract, TOP LEVEL.

    The location is the security model, not a detail. `metadata.runtime` is
    written by the worker from repo content, so anything placed there carries
    only repo trust and is classified `derived` -- refused unless it happens to
    match the reviewed registry allowlist. Top-level task metadata was set
    through an authenticated hub credential, which is what earns the
    `hub_declared` tier. A CLI that wrote to the wrong subtree would quietly
    defeat the whole distinction, so it is asserted in the tests rather than
    left to a reader's care.
    """
    task = cp.get_task(task_id)
    record = task.to_dict() if hasattr(task, "to_dict") else dict(task)
    metadata = dict(record.get("metadata") or {})
    block = dict(metadata.get("egress_contract") or {})
    block["hosts"] = hosts
    if reason:
        block["reason"] = reason
    metadata["egress_contract"] = block
    return cp.update_task(task_id, metadata=metadata, actor=args.actor)


def _iso_seconds_ago(seconds: float) -> str:
    """An ISO cursor a little in the past.

    The wait lists tasks and then starts polling events. A transition landing
    between those two calls falls in the gap and is never seen, so the cursor
    starts behind the listing rather than at it. Re-delivered events are
    filtered by id, so overlapping is free and missing one is not.
    """
    return (datetime.now(timezone.utc) - timedelta(seconds=max(1.0, seconds))).isoformat()


def _wait_poll_events(cp: Any, cursor: str, args: argparse.Namespace) -> List[Any]:
    """One batch of task transitions since ``cursor``.

    Prefers the hub's follow stream, which blocks on the hub for up to one
    interval and returns whatever arrived. That turns a poll-per-interval into
    one held connection, and the latency stops being a client-side guess.

    Falls back to a plain query whenever the stream is unavailable -- a local
    authority has no HTTP at all, and a hub older than /events/stream answers
    404. The deployed hub is routinely behind the client, so this fallback is
    the normal path, not an edge case.
    """
    streamer = getattr(cp, "stream_events", None)
    if streamer is not None and not args.no_stream:
        collected: List[Any] = []
        try:
            for record in streamer(
                subject_type="task",
                event_type="task.transitioned",
                since=cursor,
                timeout_seconds=args.poll_interval,
                poll_interval_seconds=min(1.0, args.poll_interval),
            ):
                collected.append(record)
        except MACError:
            # Stream refused or dropped: fall through to the query below rather
            # than ending the wait. The rescan reconciles anything missed.
            pass
        else:
            return collected
    time.sleep(args.poll_interval)
    try:
        return list(
            cp.list_events(
                subject_type="task",
                event_type="task.transitioned",
                since=cursor,
                limit=500,
            )
        )
    except MACError:
        # A hub blip must not end the wait; the rescan reconciles.
        return []


def cmd_task_wait(args: argparse.Namespace) -> None:
    """Wait for a project's tasks to finish, streaming each transition."""
    cp = _plane(args)
    project = _effective_read_project(args)
    if not project:
        raise MACError(
            "task wait needs a project: pass --project, or run it inside a "
            "checkout whose project can be inferred. Waiting on the whole "
            "ledger would never return."
        )
    wait = TaskWait(
        waitable_tasks(cp.list_tasks(project=project), project=project),
        follow_new=not args.no_follow_new,
    )

    def emit(update: Mapping[str, Any]) -> None:
        # Newline-delimited JSON so the feed can be piped into something while
        # it runs. A single JSON array would not be readable until the wait
        # ended, which defeats the point of a feed. The closing summary goes out
        # the same way rather than through _print: mixing a pretty-printed
        # object into a stream of one-line records makes the whole output
        # unparseable by anything reading it a line at a time.
        if _OUTPUT_JSON:
            print(json.dumps(update, sort_keys=True), flush=True)
        elif update.get("schema") == WAIT_SCHEMA:
            print(
                "%s: %d finished, %d stalled, %d still pending"
                % (
                    "timed out" if update.get("timed_out") else "done",
                    len(update.get("finished") or []),
                    len(update.get("stalled") or []),
                    len(update.get("still_pending") or []),
                ),
                flush=True,
            )
            for task_id in update.get("stalled") or []:
                # Named individually: the wait returned without these
                # finishing, and a count alone reads like success.
                print("  stalled, no longer waited on: %s" % task_id, flush=True)
        else:
            print(
                "%-10s %s  %s%s"
                % (
                    update.get("state", ""),
                    update.get("task_id", ""),
                    update.get("event", ""),
                    (" (%s)" % update["reason"]) if update.get("reason") else "",
                ),
                flush=True,
            )

    for task_id, state in sorted(wait.pending.items()):
        emit({"task_id": task_id, "state": state, "event": "waiting"})

    deadline = time.monotonic() + args.timeout if args.timeout else None
    seen_events: set = set()
    # Start the cursor slightly in the past: a transition that lands between
    # the initial listing and the first poll would otherwise fall in the gap
    # between them and never be seen.
    cursor = _iso_seconds_ago(args.poll_interval * 2)
    last_rescan = time.monotonic()

    while not wait.done:
        if deadline is not None and time.monotonic() >= deadline:
            result = wait.summary()
            result["timed_out"] = True
            emit(result)
            raise SystemExit(1)
        events = _wait_poll_events(cp, cursor, args)
        for event in dedupe_events(list(reversed(list(events))), seen_events):
            created_at = str(event.get("created_at") or "")
            if created_at > cursor:
                cursor = created_at
            update = wait.apply_event(event)
            if update is not None:
                emit(update)
        # Periodic reconciliation against authoritative state. Events can be
        # missed -- a hub restart, a dropped poll -- and a wait that hangs
        # forever on a task that finished unobserved is the failure that sends
        # people back to polling by hand.
        if time.monotonic() - last_rescan >= args.rescan_interval:
            last_rescan = time.monotonic()
            for update in wait.rescan(
                cp.list_tasks(project=project), project=project
            ):
                emit(update)

    emit(wait.summary())

def _project_egress_block(cp: Any, project: str) -> Dict[str, Any]:
    """The project's operator-declared egress contract, as a plain dict."""
    row = cp.store.query_one(
        "SELECT metadata FROM projects WHERE name = ?", (project,)
    ) if hasattr(cp, "store") else None
    if row is None:
        return {}
    import json as _json

    try:
        metadata = _json.loads(row["metadata"] or "{}")
    except ValueError:
        return {}
    block = metadata.get("egress_contract") if isinstance(metadata, dict) else None
    return dict(block) if isinstance(block, dict) else {}


def _write_project_egress(
    cp: Any, project: str, hosts: List[str], reason: str
) -> Dict[str, Any]:
    """Persist the host list at projects.metadata.egress_contract.

    PROJECT level, not task level, and that is the point. A repository has one
    egress contract and many tasks -- Aviation alone has hundreds -- so declaring
    it per task would mean re-declaring it forever and missing every task created
    before the declaration. The hub projects this onto each assignment at claim
    time, so it reaches existing tasks too.

    It is also the stronger owner: a task's own metadata.egress_contract is
    written by whoever created the task, whereas this is project policy an
    operator sets. The assignment projection strips any task-level value.
    """
    import json as _json

    row = cp.store.query_one("SELECT metadata FROM projects WHERE name = ?", (project,))
    if row is None:
        raise SystemExit("no such project: %s (mac project list)" % project)
    try:
        metadata = _json.loads(row["metadata"] or "{}")
    except ValueError:
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    block = dict(metadata.get("egress_contract") or {})
    block["hosts"] = hosts
    if reason:
        block["reason"] = reason
    metadata["egress_contract"] = block
    cp.store.execute(
        "UPDATE projects SET metadata = ?, updated_at = ? WHERE name = ?",
        (_json.dumps(metadata), _utcnow_iso(), project),
    )
    return block


def _utcnow_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def cmd_project_egress_list(args: argparse.Namespace) -> None:
    """Show the hosts a project declares for sandbox egress."""
    cp = _plane(args)
    block = _project_egress_block(cp, args.project)
    _print(
        {
            "project": args.project,
            "hosts": list(block.get("hosts") or []),
            "reason": block.get("reason"),
            # Named so the reader can see WHICH tier these are granted at
            # without going to read the policy module.
            "trust_tier": "hub_declared",
        }
    )


def cmd_project_egress_grant(args: argparse.Namespace) -> None:
    """Allow one host for every task in a project."""
    from mac.sandbox_egress import normalize_host

    host = normalize_host(args.host)
    if host is None:
        raise SystemExit(
            "%r is not a plain DNS hostname. Policy YAML is assembled by "
            "concatenation, so globs, ports, schemes and anything with YAML "
            "significance are refused here rather than in the renderer."
            % args.host
        )
    cp = _plane(args)
    hosts = list(_project_egress_block(cp, args.project).get("hosts") or [])
    if host not in hosts:
        hosts.append(host)
    _print(_write_project_egress(cp, args.project, sorted(hosts), args.reason or ""))


def cmd_project_egress_revoke(args: argparse.Namespace) -> None:
    """Withdraw one host from a project's declared egress."""
    cp = _plane(args)
    hosts = [
        h
        for h in (_project_egress_block(cp, args.project).get("hosts") or [])
        if h != args.host
    ]
    _print(_write_project_egress(cp, args.project, hosts, ""))


def cmd_task_egress_list(args: argparse.Namespace) -> None:
    """Show the hosts a task declares for sandbox egress."""
    cp = _plane(args)
    block = _egress_contract_block(cp.get_task(args.task_id))
    _print(
        {
            "task_id": args.task_id,
            "hosts": list(block.get("hosts") or []),
            "reason": block.get("reason"),
            # Named so the reader can see WHICH tier these will be granted at
            # without going to read the policy module.
            "trust_tier": "hub_declared",
            "source": "metadata.egress_contract",
        }
    )


def cmd_task_egress_grant(args: argparse.Namespace) -> None:
    """Add a host to a task's declared egress contract."""
    from mac.sandbox_egress import normalize_host

    cp = _plane(args)
    normalized = normalize_host(args.host)
    if normalized is None:
        # Rejected HERE rather than silently dropped at sandbox build time,
        # where the operator would see a host they granted simply not work.
        raise MACError(
            "%r is not a plain DNS hostname. Schemes, ports, paths, globs and "
            "IP literals are refused: egress policy is keyed on the DNS name."
            % args.host
        )
    block = _egress_contract_block(cp.get_task(args.task_id))
    hosts = list(block.get("hosts") or [])
    if normalized in hosts:
        _print({"task_id": args.task_id, "hosts": hosts, "unchanged": True})
        return
    hosts.append(normalized)
    _print(_write_egress_hosts(cp, args.task_id, sorted(hosts), args, args.reason or ""))


def cmd_task_egress_revoke(args: argparse.Namespace) -> None:
    """Remove a host from a task's declared egress contract."""
    from mac.sandbox_egress import normalize_host

    cp = _plane(args)
    normalized = normalize_host(args.host) or str(args.host).strip().lower()
    block = _egress_contract_block(cp.get_task(args.task_id))
    hosts = [h for h in (block.get("hosts") or []) if h != normalized]
    if len(hosts) == len(block.get("hosts") or []):
        raise MACError(
            "%s is not in the task's declared egress contract" % normalized
        )
    _print(_write_egress_hosts(cp, args.task_id, hosts, args, block.get("reason") or ""))


def cmd_task_update(args: argparse.Namespace) -> None:
    """Change a task's fields -- the CRUD `update` the CLI never exposed.

    ControlPlane.update_task has always existed; nothing called it from the
    command line, so the closest thing a user could find was `task edit`, which
    is a different operation entirely: it opens $EDITOR to ANSWER a task parked
    on a human question, and refuses unless the task is in NEEDS_INPUT. Aliasing
    `update` onto that would have pointed the most predictable verb in the
    vocabulary at something that does not do what it says.

    Only the flags supplied are changed; everything omitted is left alone.
    """
    fields = {
        "title": args.title,
        "description": args.description,
        "project": args.project,
        "priority": args.priority,
        "max_attempts": args.max_attempts,
    }
    if args.capabilities is not None:
        fields["required_capabilities"] = [
            item.strip() for item in args.capabilities.split(",") if item.strip()
        ]
    if args.description is None and getattr(args, "description_file", None):
        fields["description"] = _read_text_arg(
            None, args.description_file, label="--description", default=""
        )
    if args.metadata is not None or getattr(args, "metadata_file", None):
        fields["metadata"] = _read_json_arg(
            args.metadata,
            getattr(args, "metadata_file", None),
            label="--metadata",
            default={},
        )
    supplied = {key: value for key, value in fields.items() if value is not None}
    if not supplied:
        raise SystemExit(
            "nothing to update: supply at least one of --title, --description, "
            "--project, --priority, --max-attempts, --capabilities, --metadata"
        )
    _print(
        _plane(args).update_task(args.task_id, actor=args.actor, **supplied)
    )


def cmd_agent_show(args: argparse.Namespace) -> None:
    """Read one agent -- the CRUD `show` every other first-class object had.

    `mac agent config show` already existed and is richer (identity, runtime
    flags, gateway-reported deploy config, mood), but it is a different
    question. This is the plain read that makes the object's vocabulary
    uniform: create, list, show, update, delete.
    """
    _print(_plane(args).get_agent(args.agent_id))


def cmd_agent_list(args: argparse.Namespace) -> None:
    cp = _plane(args)
    rows = [agent.to_dict() if hasattr(agent, "to_dict") else dict(agent) for agent in cp.list_agents()]
    if getattr(args, "health", False):
        age_helper = getattr(cp, "unconsumed_control_stream_age_seconds", None)
        for row in rows:
            age: Optional[float]
            if callable(age_helper):
                try:
                    age = age_helper(str(row.get("id") or ""))
                except Exception:  # noqa: BLE001 - fall back to response timestamps below.
                    age = _agent_unconsumed_control_stream_age_from_row(row)
            else:
                age = _agent_unconsumed_control_stream_age_from_row(row)
            row["dispatch_hold"] = bool(row.get("dispatch_hold", False))
            row["unconsumed_control_stream_age_seconds"] = age
    _print(_apply_selector(rows, args, "agent"))


def cmd_agent_attestation_recover(args: argparse.Namespace) -> None:
    """Conditionally recover a missing/stale key into an owner-only manifest.

    The cleartext key is never rendered by the CLI. Fleet deploy relays the
    manifest under its target-side deployment lock and consumes it only after a
    second signed proof succeeds.
    """

    from mac.deployment_attestation import _atomic_private_json, recovery_manifest

    probe_path = Path(args.probe_file).expanduser()
    probe = json.loads(probe_path.read_text(encoding="utf-8"))
    if not isinstance(probe, dict):
        raise MACError("attestation recovery probe must be a JSON object")
    result = _plane(args).recover_agent_attestation_key(args.agent_id, probe)
    payload = result.to_dict() if hasattr(result, "to_dict") else result
    key = payload if isinstance(payload, str) else payload.get("attestation_key")
    manifest = recovery_manifest(
        args.agent_id,
        str(probe.get("deployment_id") or ""),
        str(key or ""),
    )
    destination = Path(args.manifest_out).expanduser()
    _atomic_private_json(destination, manifest)
    _print(
        {
            "status": "rotation_manifest_written",
            "agent_id": args.agent_id,
            "deployment_id": manifest["deployment_id"],
            "manifest": str(destination),
        }
    )


def cmd_agent_report_executor_approve(args: argparse.Namespace) -> None:
    attestation = json.loads(
        Path(args.attestation_file).expanduser().read_text(encoding="utf-8")
    )
    if not isinstance(attestation, dict):
        raise MACError("report executor attestation must be a JSON object")
    _print(
        _plane(args).approve_agent_report_repository_executor(
            args.agent_id,
            attestation,
            args.startup_timestamp,
            actor=args.actor,
        )
    )


def cmd_agent_report_executor_revoke(args: argparse.Namespace) -> None:
    _print(
        _plane(args).revoke_agent_report_repository_executor(
            args.agent_id,
            args.reason,
            actor=args.actor,
        )
    )


def _agent_unconsumed_control_stream_age_from_row(row: Mapping[str, Any]) -> Optional[float]:
    published_at = row.get("last_control_stream_published_at")
    if not published_at:
        return None
    try:
        published = parse_time(str(published_at))
        consumed_at = row.get("last_control_stream_consumed_at")
        if consumed_at and parse_time(str(consumed_at)) >= published:
            return None
        return max(0.0, (parse_time(utcnow()) - published).total_seconds())
    except Exception:  # noqa: BLE001 - malformed hub data should not break list output.
        return None


def cmd_agent_reflect(args: argparse.Namespace) -> None:
    _print(
        _plane(args).publish_agent_reflection(
            args.agent_id,
            recipient_agent_id=args.recipient_agent_id,
            request_id=args.request_id,
            reflect_timeout=args.reflect_timeout,
        )
    )


def cmd_agent_delete(args: argparse.Namespace) -> None:
    """Hard-delete an agent record (mood/nap/events/messages); task history is
    task-keyed and preserved. Refused while the agent holds an active lease."""
    _plane(args).delete_agent(args.agent_id, actor=args.actor or "human")
    _print({"deleted": args.agent_id})


def cmd_agent_migrate(args: argparse.Namespace) -> None:
    """Move an agent (soul + memory) to a new host. Dry-run by default; pass
    --execute to run the backup -> retarget -> deploy -> restore -> verify
    playbook. The agent NAME is preserved, so its hub-stored persona / memories
    / mood follow ``agent_<name>`` automatically."""
    import shutil
    import time
    from dataclasses import replace

    import yaml

    from mac import agent_migrate as am
    from mac.fleet_deploy import canonicalize_mesh_ssh_target, parse_ssh_target
    from mac.fleet_ssh import FleetSshError, resolve_fleet_ssh
    from mac.hermes_config_surface import registry_path

    reg_path = registry_path()
    registry = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or {}
    fleets = registry.get("fleets") or {}
    fleet = args.fleet or next(
        (f for f, d in fleets.items()
         if any((a or {}).get("name") == args.name for a in (d.get("agents") or []))),
        None,
    )
    if not fleet or fleet not in fleets:
        raise SystemExit("agent %r not found in any fleet in %s" % (args.name, reg_path))
    agents = fleets[fleet].get("agents") or []
    cur = next((a for a in agents if (a or {}).get("name") == args.name), None)
    if cur is None:
        raise SystemExit("agent %r not in fleet %r" % (args.name, fleet))
    src = args.from_target or cur.get("target")
    if not src:
        raise SystemExit("no source target for %r; pass --from" % args.name)

    # Hub migration moves the durable hub state (DB + Qdrant + secret key), not
    # just the soul. Auto-detect when the agent IS the fleet's hub/shared-service
    # manager; --hub/--no-hub override.
    fleet_cfg = fleets[fleet]
    is_hub_agent = args.name in (
        fleet_cfg.get("hub_agent"),
        fleet_cfg.get("shared_services_manager_agent"),
    )
    hub = is_hub_agent if args.hub is None else args.hub
    src_os = args.src_os or (cur.get("os") or "linux")
    network = (fleet_cfg.get("defaults") or {}).get("network") or {}
    network_provider = str(network.get("provider") or "none")

    try:
        src_route = resolve_fleet_ssh(registry, fleet, args.name)
        parsed_src = parse_ssh_target(str(src), port=src_route.port)
        src_route = replace(
            src_route, target=parsed_src.user_host, port=parsed_src.port
        )
        dst_target = canonicalize_mesh_ssh_target(
            args.to_target,
            provider=network_provider,
            port=args.to_ssh_port,
        )
        parsed_dst = parse_ssh_target(dst_target)
        dst_route = replace(
            src_route,
            target=parsed_dst.user_host,
            port=parsed_dst.port,
            identity_file=(
                str(Path(args.to_identity_file).expanduser())
                if args.to_identity_file
                else src_route.identity_file
            ),
            proxy_jump=(
                args.to_proxy_jump
                if args.to_proxy_jump is not None
                else src_route.proxy_jump
            ),
            known_hosts_file=(
                str(Path(args.to_known_hosts_file).expanduser())
                if args.to_known_hosts_file
                else src_route.known_hosts_file
            ),
            host_key_policy=args.to_host_key_policy or src_route.host_key_policy,
            os_kind=args.to_os,
        )
        src_route.validate_portable()
        dst_route.validate_portable()
    except (FleetSshError, ValueError) as exc:
        raise SystemExit("could not resolve migration SSH routes: %s" % exc) from exc

    steps = am.migration_plan(
        args.name,
        src_target=src,
        dst_target=dst_target,
        fleet=fleet,
        fleet_name=(fleet_cfg.get("fleet_name") or fleet),
        to_os=args.to_os,
        src_os=src_os,
        keep_source=args.keep_source,
        retire_source_agent=args.retire_source_agent,
        hub=hub,
        src_route=src_route,
        dst_route=dst_route,
    )
    if hub:
        print("# HUB migration: moving soul + mac.db + Qdrant + MAC_SECRET_KEY/MAC_API_TOKEN")
    if not args.execute:
        print(am.render_plan(args.name, steps))
        return
    # --execute: retarget fleets.yaml (backup first), then run the playbook.
    backup = "%s.bak.%d" % (reg_path, int(time.time()))
    shutil.copy2(reg_path, backup)
    am.retarget_fleet_agent(registry, fleet, args.name, target=dst_target, os=args.to_os)
    reg_path.write_text(yaml.safe_dump(registry, sort_keys=False), encoding="utf-8")
    print("retargeted %s -> %s in %s (backup: %s)" % (args.name, dst_target, reg_path, backup))
    _print(am.execute_migration(args.name, steps))


def cmd_agent_hardware(args: argparse.Namespace) -> None:
    """Fleet hardware inventory from self-reported resources["hardware"], with the
    hub-derived gen capability (can this agent host media generation, and is it
    currently advertising a gen endpoint)."""
    from mac.hardware import summarize
    from mac.local_gen_catalog import models_for_hardware
    from mac.media_routing import is_gen_capable

    rows = []
    for agent in _plane(args).list_agents():
        data = agent.to_dict() if hasattr(agent, "to_dict") else agent
        resources = data.get("resources") if isinstance(data.get("resources"), dict) else {}
        hardware = resources.get("hardware") if isinstance(resources, dict) else None
        serving = bool(isinstance(resources, dict) and resources.get("media_routes"))
        capable = is_gen_capable(hardware) if isinstance(hardware, dict) else False
        # Routable-today (image) catalog models this agent's hardware can run.
        runnable = [m.id for m in models_for_hardware(hardware) if m.routable] if isinstance(hardware, dict) else []
        rows.append({
            "agent": data.get("name") or data.get("id"),
            "accelerator": (hardware or {}).get("accelerator", "unknown") if isinstance(hardware, dict) else "unreported",
            "gen": ("serving" if serving else "capable") if capable else ("serving(cpu)" if serving else "no"),
            "runnable_models": runnable,
            "hardware": summarize(hardware) if isinstance(hardware, dict) else "(no hardware reported — agent predates self-reporting; redeploy to populate)",
        })
    _print(rows)


def cmd_agent_heartbeat(args: argparse.Namespace) -> None:
    _print(
        _plane(args).heartbeat_agent(
            args.agent_id,
            status=args.status,
            health_status=args.health_status,
            resources=_json_arg(args.resources, None),
            running_digest=args.running_digest,
        )
    )


def cmd_agent_hold(args: argparse.Namespace) -> None:
    """Place a dispatch hold on an agent; the agent will be skipped during claim-next."""
    _print(_plane(args).set_agent_dispatch_hold(args.agent_id, args.reason))


def cmd_agent_resume(args: argparse.Namespace) -> None:
    """Remove the dispatch hold from an agent, making it eligible for dispatch again."""
    _print(_plane(args).clear_agent_dispatch_hold(args.agent_id))


def cmd_agent_tell(args: argparse.Namespace) -> None:
    """Hub-verified human directive to ANY agent (no Slack identity needed)."""
    cp = _plane(args)
    agent_id = _resolve_agent_id(cp, args.agent)
    kwargs = {"target_agent_id": agent_id, "message": args.message}
    if args.issued_by:
        kwargs["issued_by"] = args.issued_by
    if args.wait:
        kwargs["wait_seconds"] = args.wait
    if getattr(args, "task", None):
        kwargs["task_id"] = args.task
    try:
        result = cp.publish_human_directive(**kwargs)
    except TypeError:
        # Local (in-process) plane: no hub-side wait loop; publish only.
        kwargs.pop("wait_seconds", None)
        target = kwargs.pop("target_agent_id")
        message = kwargs.pop("message")
        result = cp.publish_human_directive(target, message, **kwargs)
    _print(result.to_dict() if hasattr(result, "to_dict") else result)


def cmd_agent_deregister(args: argparse.Namespace) -> None:
    """Graceful exit for a session/ephemeral agent: optional final message,
    then tombstone with history preserved."""
    cp = _plane(args)
    result = cp.deregister_agent(
        args.agent_id,
        actor=args.actor,
        final_message=args.final_message,
        final_target=args.final_target,
    )
    _print(result.to_dict() if hasattr(result, "to_dict") else result)


def _resolve_agent_id(cp: Any, agent: str) -> str:
    """Accept an agent id or a human name; return the id."""
    candidates = []
    for row in cp.list_agents():
        data = row.to_dict() if hasattr(row, "to_dict") else dict(row)
        if data.get("id") == agent:
            return agent
        if data.get("name") == agent:
            candidates.append(str(data.get("id")))
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        raise SystemExit(
            "agent name %r is ambiguous (%s); pass the agent id"
            % (agent, ", ".join(candidates))
        )
    return agent


def cmd_agent_config_show(args: argparse.Namespace) -> None:
    """Consolidated per-agent config: identity + flags + deploy doc + mood."""
    cp = _plane(args)
    agent_id = _resolve_agent_id(cp, args.agent)
    view = cp.effective_agent_config(agent_id)
    if hasattr(view, "to_dict"):
        view = view.to_dict()
    # The whole point of this command is showing every knob; the compact
    # text renderer would collapse the nested sections to "{n keys}".
    print(json.dumps(view, indent=2, sort_keys=True, default=str))


def cmd_fleet_build_distribution(args: argparse.Namespace) -> None:
    _print(_plane(args).fleet_build_distribution())


def _fleet_target_path(args: argparse.Namespace) -> Optional[Path]:
    manifest = getattr(args, "manifest", None)
    return Path(manifest) if manifest else None


def cmd_fleet_target_set(args: argparse.Namespace) -> None:
    """Pin the target of record for a role, then print the stored role target."""
    from mac import fleet_target as ft

    path = _fleet_target_path(args)
    try:
        manifest = ft.load_manifest(path)
    except ft.FleetTargetError:
        # First write establishes the manifest.
        manifest = ft.FleetTargetManifest()
    openclaw = None
    if args.openclaw_version or args.openclaw_revision:
        if not (args.openclaw_version and args.openclaw_revision):
            raise MACError(
                "--openclaw-version and --openclaw-revision must be set together"
            )
        openclaw = ft.OpenClawTrack(
            version=args.openclaw_version, revision=args.openclaw_revision
        )
    manifest.set_role(
        args.role,
        ft.RoleTarget(
            source=ft.normalize_commit(args.source), openclaw=openclaw
        ),
    )
    ft.save_manifest(manifest, path)
    _print({"role": args.role, "target": manifest.get_role(args.role).to_dict()})


def cmd_fleet_target_get(args: argparse.Namespace) -> None:
    """Print the pinned target for a single role."""
    from mac import fleet_target as ft

    manifest = ft.load_manifest(_fleet_target_path(args))
    _print({"role": args.role, "target": manifest.get_role(args.role).to_dict()})


def cmd_fleet_target_show(args: argparse.Namespace) -> None:
    """Print the full fleet target-of-record manifest."""
    from mac import fleet_target as ft

    _print(ft.load_manifest(_fleet_target_path(args)).to_dict())


def cmd_fleet_target_list(args: argparse.Namespace) -> None:
    """List every pinned role target (never empty once the manifest is populated)."""
    from mac import fleet_target as ft

    manifest = ft.load_manifest(_fleet_target_path(args))
    _print(
        [
            {"role": name, "target": manifest.roles[name].to_dict()}
            for name in sorted(manifest.roles)
        ]
    )



def cmd_fleet_move_agent(args: argparse.Namespace) -> None:
    """Move an agent between fleets: rewrite fleets.yaml + optionally redeploy.

    Dry-run by default (prints the plan).  Pass --execute to actually mutate
    fleets.yaml, create a backup, and print the redeploy + DB reconcile commands.
    """
    from mac.fleet_move import (
        execute_fleet_move,
        find_agent_fleet,
        fleet_hub_url,
        plan_fleet_move,
        render_move_plan,
        resolve_fleet_key,
    )
    from mac.hermes_config_surface import registry_path

    reg_path = registry_path()

    try:
        import yaml  # type: ignore[import]
    except ImportError as exc:
        raise SystemExit("PyYAML is required for fleet move-agent") from exc

    registry = yaml.safe_load(reg_path.read_text(encoding="utf-8")) or {}

    agent_name = args.agent

    # Resolve --from: explicit (registry KEY or fleet_name), else auto-detect.
    if args.from_fleet:
        from_fleet = resolve_fleet_key(registry, args.from_fleet)
        if not from_fleet:
            raise SystemExit(
                "source fleet %r not found in %s (by registry key or fleet_name)"
                % (args.from_fleet, reg_path)
            )
    else:
        from_fleet = find_agent_fleet(registry, agent_name)
        if not from_fleet:
            raise SystemExit(
                "agent %r not found in any fleet in %s; "
                "pass --from to specify the source fleet" % (agent_name, reg_path)
            )
        print("auto-detected source fleet: %s" % from_fleet)

    # Resolve --to (registry KEY or fleet_name); fail loudly — never emit a
    # "<target-hub-url>" placeholder plan for an unresolvable / hubless target.
    to_fleet = resolve_fleet_key(registry, args.to_fleet)
    if not to_fleet:
        raise SystemExit(
            "target fleet %r not found in %s (by registry key or fleet_name)"
            % (args.to_fleet, reg_path)
        )
    if not ((args.hub_url or "").strip() or fleet_hub_url(registry, to_fleet)):
        raise SystemExit(
            "target fleet %r has no hub_url (pass --hub-url to override)" % to_fleet
        )
    if args.from_fleet not in (None, from_fleet) or args.to_fleet != to_fleet:
        print("resolved fleets: %s -> %s" % (from_fleet, to_fleet))

    if not args.execute:
        # Dry-run: print the plan and the proposed registry diff.
        steps = plan_fleet_move(agent_name, from_fleet, to_fleet, registry,
                                reconcile_db=not args.no_db_reconcile)
        print(render_move_plan(agent_name, from_fleet, to_fleet, steps))
        return

    result = execute_fleet_move(
        agent_name,
        from_fleet,
        to_fleet,
        fleets_config=reg_path,
        to_os=args.to_os,
        dry_run=False,
        reconcile_db=not args.no_db_reconcile,
        hub_url=args.hub_url or None,
        run_redeploy=not args.no_redeploy,
    )

    if not result.get("ok"):
        if result.get("registry_written"):
            # The move landed in fleets.yaml but the live redeploy failed —
            # surface both so the operator can re-run or revert from the backup.
            print("registry moved (%s -> %s); backup: %s"
                  % (from_fleet, to_fleet, result.get("backup")))
            print("redeploy FAILED (rc=%s); re-run: %s"
                  % (result.get("redeploy_returncode"), result.get("redeploy_cmd")))
        raise SystemExit("fleet move-agent failed: %s" % result.get("error"))

    if result.get("idempotent"):
        print(result["message"])
        return

    print("agent %r moved: %s -> %s" % (agent_name, from_fleet, to_fleet))
    if result.get("backup"):
        print("registry backed up to %s" % result["backup"])
    if result.get("registry_written"):
        print("registry written to %s" % result["registry_written"])
    if result.get("redeployed"):
        print("redeployed at hub %s (--hub %s)"
              % (result.get("target_hub_url"), to_fleet))
    if result.get("db_reconcile"):
        print("DB: %s" % result["db_reconcile"])
    for step in result.get("next_steps") or []:
        print("next: %s" % step)


def _sender_agent_id(args: argparse.Namespace) -> str:
    sender = (
        getattr(args, "sender_agent_id", None)
        or os.environ.get("MAC_AGENT_ID")
        or os.environ.get("MAC_WORKER_AGENT_ID")
    )
    if not sender:
        raise MACError(
            "admin/control sender agent id is required; pass --sender-agent-id or set MAC_AGENT_ID"
        )
    return sender


def cmd_fleet_refresh_source(args: argparse.Namespace) -> None:
    recipients = list(args.agent_id or [])
    _print(
        _plane(args).publish_agentbus_repo_update(
            sender_agent_id=_sender_agent_id(args),
            recipient_agent_ids=recipients,
            all_agents=not recipients,
            repo_path=args.repo_path,
            remote=args.remote,
            branch=args.branch,
            restart=not args.no_restart,
            restart_services=list(args.restart_service or []),
            request_id=args.request_id,
        )
    )


def cmd_fleet_snapshot(args: argparse.Namespace) -> None:
    """fleet-02: the team roster + what each agent is doing now."""
    _print(_plane(args).fleet_snapshot(exclude_agent_id=getattr(args, "agent", None)))


def _read_directive_document(path: str) -> Dict[str, Any]:
    import yaml

    raw = _read_text_arg(None, path, label="directive document")
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise SystemExit("directive document is invalid YAML/JSON: %s" % exc)
    if not isinstance(value, dict):
        raise SystemExit("directive document must be a YAML/JSON object")
    return value


def cmd_directive_propose(args: argparse.Namespace) -> None:
    _print(
        _plane(args).propose_directive(
            _read_directive_document(args.document_file), actor=args.actor
        )
    )


def cmd_directive_list(args: argparse.Namespace) -> None:
    _print(_plane(args).list_directives(state=args.state))


def cmd_directive_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_directive(args.directive))


def cmd_directive_versions(args: argparse.Namespace) -> None:
    _print(_plane(args).list_directive_versions(args.directive))


def cmd_directive_check(args: argparse.Namespace) -> None:
    _print(
        _plane(args).check_directive(
            args.directive, version=args.version, actor=args.actor
        )
    )


def cmd_directive_impact(args: argparse.Namespace) -> None:
    _print(_plane(args).directive_impact(args.directive))


def cmd_directive_approve(args: argparse.Namespace) -> None:
    _print(
        _plane(args).approve_directive(
            args.directive,
            version=args.version,
            directive_digest=args.digest,
            check_id=args.check_id,
            actor=args.actor,
        )
    )


def cmd_directive_activate(args: argparse.Namespace) -> None:
    _print(
        _plane(args).activate_directive(
            args.directive,
            version=args.version,
            directive_digest=args.digest,
            actor=args.actor,
        )
    )


def cmd_directive_deactivate(args: argparse.Namespace) -> None:
    reason = _read_text_arg(args.reason, args.reason_file, label="reason")
    if not reason.strip():
        raise SystemExit("directive deactivate requires --reason or --reason-file")
    _print(
        _plane(args).deactivate_directive(
            args.directive, reason=reason, actor=args.actor
        )
    )


def cmd_directive_effective(args: argparse.Namespace) -> None:
    _print(
        _plane(args).effective_directives(
            repository_id=args.repository_id,
            project=args.project,
        )
    )


def cmd_directive_binding_set(args: argparse.Namespace) -> None:
    value = _read_json_arg(
        args.value,
        args.value_file,
        label="directive binding value",
        default=None,
    )
    if value is None:
        raise SystemExit("directive binding set requires --value or --value-file")
    _print(
        _plane(args).set_directive_binding(
            target_type=args.target_type,
            target_id=args.target_id,
            key=args.key,
            value=value,
            actor=args.actor,
        )
    )


def cmd_directive_binding_list(args: argparse.Namespace) -> None:
    _print(
        _plane(args).list_directive_bindings(
            target_type=args.target_type,
            target_id=args.target_id,
            active=not args.inactive,
        )
    )


def cmd_directive_waiver_create(args: argparse.Namespace) -> None:
    reason = _read_text_arg(args.reason, args.reason_file, label="waiver reason")
    if not reason.strip():
        raise SystemExit("directive waiver create requires --reason or --reason-file")
    _print(
        _plane(args).create_directive_waiver(
            args.directive,
            version=args.version,
            target_type=args.target_type,
            target_id=args.target_id,
            reason=reason,
            expires_at=args.expires_at,
            actor=args.actor,
        )
    )


def cmd_directive_waiver_list(args: argparse.Namespace) -> None:
    _print(_plane(args).list_directive_waivers(directive_id=args.directive))


def cmd_directive_waiver_revoke(args: argparse.Namespace) -> None:
    reason = _read_text_arg(args.reason, args.reason_file, label="waiver revoke reason")
    if not reason.strip():
        raise SystemExit("directive waiver revoke requires --reason or --reason-file")
    _print(
        _plane(args).revoke_directive_waiver(
            args.waiver, actor=args.actor, reason=reason
        )
    )


def cmd_openshell_render_policy(args: argparse.Namespace) -> None:
    """Render the OpenShell guardrail policy from the operator template + fleet
    values; install at ~/.mac/openshell-policy.yaml (or print). The policy half
    of executor sandbox enforcement (flipping it on additionally needs the
    Hermes-runtime sandbox image)."""
    from mac import openshell_policy as _op

    template = Path(args.template).expanduser().read_text(encoding="utf-8")
    rendered = _op.render_policy(
        template,
        agent_user=args.agent_user,
        hub_host=args.hub_host,
        hub_port=args.hub_port,
        model_gateway_host=getattr(args, "model_gateway_host", None),
        shared_services={"qdrant": args.qdrant_port, "firecrawl": args.firecrawl_port},
        image_runtime=getattr(args, "image_runtime", None),
    )
    if args.into:
        dest = Path(args.into).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered, encoding="utf-8")
        dest.chmod(0o600)
        _print({"wrote": str(dest), "bytes": len(rendered)})
    else:
        sys.stdout.write(rendered)


def cmd_openshell_policy_create(args: argparse.Namespace) -> None:
    policy_text = _read_text_arg(args.policy_text, args.policy_file, label="policy")
    _print(
        _plane(args).create_openshell_policy(
            args.name,
            policy_text,
            description=args.description or "",
            metadata=_read_json_arg(args.metadata, args.metadata_file, label="metadata", default={}),
            created_by=args.created_by,
            policy_id=args.policy_id,
        )
    )


def cmd_openshell_policy_list(args: argparse.Namespace) -> None:
    _print(
        [
            policy.to_dict() if hasattr(policy, "to_dict") else policy
            for policy in _plane(args).list_openshell_policies(
                include_deleted=args.include_deleted
            )
        ]
    )


def cmd_openshell_policy_show(args: argparse.Namespace) -> None:
    # `policy_text` is omitted from OpenShellPolicy.to_dict() by default, and
    # showing the policy body is the whole point of this command -- so ask for
    # it explicitly. In hub mode the value is already a dict from the
    # admin-gated endpoint and passes through unchanged; this branch is the
    # direct-authority (--db) path, where _print would otherwise serialize the
    # dataclass with its text stripped.
    _print(
        _policy_payload_with_text(
            _plane(args).get_openshell_policy(args.policy, include_deleted=True)
        )
    )


def cmd_openshell_policy_update(args: argparse.Namespace) -> None:
    policy_text = _read_text_arg(args.policy_text, args.policy_file, label="policy", default="")
    metadata = None
    if args.metadata is not None or args.metadata_file is not None:
        metadata = _read_json_arg(args.metadata, args.metadata_file, label="metadata", default={})
    _print(
        _plane(args).update_openshell_policy(
            args.policy,
            name=args.name,
            description=args.description,
            policy_text=policy_text or None,
            metadata=metadata,
            updated_by=args.updated_by,
        )
    )


def cmd_openshell_policy_delete(args: argparse.Namespace) -> None:
    _print(_plane(args).delete_openshell_policy(args.policy, actor=args.actor))


def cmd_openshell_policy_render(args: argparse.Namespace) -> None:
    shared = _read_json_arg(args.shared_services, args.shared_services_file, label="shared_services", default={})
    rendered = _plane(args).render_openshell_policy(
        args.policy,
        agent_user=args.agent_user,
        hub_host=args.hub_host,
        hub_port=args.hub_port,
        model_gateway_host=args.model_gateway_host,
        shared_services=shared,
    )
    if args.into:
        dest = Path(args.into).expanduser()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(rendered["policy_text"], encoding="utf-8")
        dest.chmod(0o600)
        _print({**rendered, "wrote": str(dest), "policy_text": ""})
    else:
        _print(rendered)


def cmd_openshell_policy_assign(args: argparse.Namespace) -> None:
    _print(
        _plane(args).assign_openshell_policy(
            args.policy,
            target_type=args.target_type,
            target_id=args.target_id,
            created_by=args.created_by,
        )
    )


def _policy_payload_with_text(value: Any) -> Any:
    """Serialize a policy/version for an operator command that needs its body.

    ``to_dict()`` omits ``policy_text`` by default (it is the guardrail source);
    the direct-authority path therefore has to ask for it. Hub-mode ``_Dictish``
    wrappers take no keyword and already carry whatever the admin-gated endpoint
    permitted, so they fall through unchanged.
    """
    try:
        return value.to_dict(include_text=True)
    except (AttributeError, TypeError):
        return value


def cmd_openshell_policy_versions(args: argparse.Namespace) -> None:
    # Diffing guardrail changes across versions requires the bodies.
    _print(
        [
            _policy_payload_with_text(version)
            for version in _plane(args).list_openshell_policy_versions(args.policy)
        ]
    )


def cmd_openshell_status(args: argparse.Namespace) -> None:
    _print(_plane(args).get_openshell_status(args.agent))


def cmd_openshell_policy_deploy_status(args: argparse.Namespace) -> None:
    _print(_plane(args).get_openshell_status(args.agent))


def cmd_openshell_reconcile(args: argparse.Namespace) -> None:
    from mac.openshell_reconcile import (
        default_fleets_path,
        fleet_agent_names,
        load_fleet_config,
        reconcile_openshell_agents,
    )

    policy_text = None
    if args.policy_file:
        policy_text = Path(args.policy_file).expanduser().read_text(encoding="utf-8")
    detail = _read_json_arg(args.detail, args.detail_file, label="detail", default={})
    selected_agents = list(args.agent or [])
    explicit_agents = bool(selected_agents)
    if not selected_agents:
        cfg_path = Path(args.fleet_config).expanduser() if args.fleet_config else default_fleets_path()
        cfg = load_fleet_config(cfg_path)
        selected_agents = fleet_agent_names(cfg, args.target_fleet or args.fleet)
    if args.apply and not args.no_report_status and args.status == "active" and not args.validated:
        raise SystemExit("--validated is required with --apply when reporting status=active")
    _print(
        reconcile_openshell_agents(
            _plane(args),
            agent_selectors=selected_agents,
            policy_name=args.policy_name,
            policy_text=policy_text,
            apply=args.apply,
            actor=args.actor,
            status=args.status,
            validated=args.validated,
            sandbox_id=args.sandbox_id,
            detail=detail,
            runtime=args.runtime,
            openshell_version=args.openshell_version,
            gateway_driver=args.gateway_driver,
            image=args.image,
            validation_summary=args.validation_summary or "",
            report_status=not args.no_report_status,
            allow_missing_agents=(not explicit_agents and not args.strict),
        )
    )


def cmd_openshell_sandbox_gc(args: argparse.Namespace) -> None:
    from mac.openshell_sandbox_gc import reconcile_stale_sandboxes

    _print(
        reconcile_stale_sandboxes(
            openshell_bin=args.openshell_bin,
            stale_after_seconds=max(0.0, args.stale_after_hours * 3600.0),
            include_legacy=not args.no_legacy,
            apply=args.apply,
        )
    )


def cmd_openshell_reap_orphans(args: argparse.Namespace) -> None:
    from mac.openshell_sandbox_gc import reap_orphaned_task_sandboxes

    _print(
        reap_orphaned_task_sandboxes(
            openshell_bin=args.openshell_bin,
            apply=args.apply,
        )
    )


def _soul_snapshot_setup(args):
    """Resolve (fleet_name, agents, transport) for the soul pull/push commands."""
    import yaml as _yaml
    from mac import soul_snapshot as _ss

    cfg = _yaml.safe_load(Path(args.fleets_config).expanduser().read_text(encoding="utf-8")) or {}
    fleet_name = args.fleet or next(iter((cfg.get("fleets") or {})), None)
    if not fleet_name:
        raise SystemExit("no fleet found; pass --fleet")
    agents = _ss.load_fleet_agents(cfg, fleet_name)
    from mac.fleet_ssh import FleetSshError, resolve_fleet_ssh

    try:
        routes = {
            target: resolve_fleet_ssh(cfg, fleet_name, name)
            for name, target in agents
        }
    except FleetSshError as exc:
        raise SystemExit(str(exc)) from exc
    return fleet_name, agents, _ss.SSHTransport(routes=routes)


def cmd_fleet_soul_pull(args: argparse.Namespace) -> None:
    """Phase 1 fleet snapshot: pull each agent's editable soul text into a local tree."""
    from datetime import datetime, timezone
    import yaml as _yaml
    from mac import soul_snapshot as _ss

    fleet_name, agents, transport = _soul_snapshot_setup(args)
    dest = Path(args.into).expanduser()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = _ss.pull_snapshot(
        agents, dest, transport, fleet=fleet_name, pulled_at=stamp,
        memory_checksum=getattr(args, "memory_checksum", False),
    )
    # Phase 3: also capture hub-stored persona + mood (resolve agent ids by name).
    if getattr(args, "with_hub", False):
        hub = _plane(args)
        by_name = {}
        try:
            for a in hub.list_agents():
                ad = a.to_dict() if hasattr(a, "to_dict") else dict(a)
                if ad.get("name"):
                    by_name[ad["name"]] = ad.get("id")
        except Exception as exc:  # noqa: BLE001
            print("mac: hub agent list failed (%s); skipping persona/mood" % exc, file=sys.stderr)
        ids = [(n, by_name.get(n) or "agent_%s" % n) for n, _t in agents]
        hub_section = _ss.capture_hub_state(hub, ids, dest, pulled_at=stamp)
        manifest["hub"] = hub_section
    (dest / "manifest.yaml").write_text(_yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    summary = {
        "fleet": fleet_name, "into": str(dest), "pulled_at": stamp,
        "agents": {
            n: {
                "soul": {f: m.get("present") for f, m in a["files"].items()},
                "memory_refs": {f: m.get("bytes") for f, m in a.get("memory", {}).items()
                                if m.get("present")},
            }
            for n, a in manifest["agents"].items()
        },
    }
    if "hub" in manifest:
        summary["hub"] = {n: {"persona": s["persona"].get("present"), "mood": s["mood"].get("present")}
                          for n, s in manifest["hub"]["agents"].items()}
    _print(summary)


def cmd_fleet_soul_push(args: argparse.Namespace) -> None:
    """Phase 1 fleet snapshot: diff edited soul tree vs live, back up + write changes."""
    from datetime import datetime, timezone
    import yaml as _yaml
    from mac import soul_snapshot as _ss

    src = Path(getattr(args, "from_dir")).expanduser()
    manifest = _yaml.safe_load((src / "manifest.yaml").read_text(encoding="utf-8"))
    # Resolve current targets from the authoritative registry instead of using
    # snapshot-era hostnames, then route every SSH call through FleetSshSpec.
    cfg = _yaml.safe_load(
        Path(args.fleets_config).expanduser().read_text(encoding="utf-8")
    ) or {}
    fleet_name = args.fleet or manifest.get("fleet")
    if not fleet_name:
        raise SystemExit("snapshot has no fleet; pass --fleet")
    from mac.fleet_ssh import FleetSshError, resolve_fleet_ssh

    current_agents = dict(_ss.load_fleet_agents(cfg, fleet_name))
    routes = {}
    try:
        for agent_name, entry in (manifest.get("agents") or {}).items():
            target = current_agents.get(agent_name)
            if not target:
                raise SystemExit(
                    "agent %r is no longer present in fleet %r" % (agent_name, fleet_name)
                )
            entry["target"] = target
            routes[target] = resolve_fleet_ssh(cfg, fleet_name, agent_name)
    except FleetSshError as exc:
        raise SystemExit(str(exc)) from exc
    transport = _ss.SSHTransport(routes=routes)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    res = _ss.plan_and_push(
        src, manifest, transport, stamp=stamp,
        dry_run=args.dry_run, only_agents=(args.agent or None),
    )
    _print({
        "dry_run": res.dry_run,
        "changes": [
            {"agent": c.agent, "file": c.relpath, "status": c.status,
             "applied": c.applied, "backup": c.backup_path}
            for c in res.changes
        ],
        "to_apply": [f"{c.agent}/{c.relpath}" for c in res.to_apply],
    })



def cmd_fleet_soul_audit(args: argparse.Namespace) -> None:
    """Audit the remote ~/.hermes directory for a named agent and print the manifest."""
    from datetime import datetime, timezone
    from mac import soul_snapshot as _ss

    fleet_name, agents, transport = _soul_snapshot_setup(args)
    agent_name = args.agent
    # Resolve agent name to SSH target
    agent_map = dict(agents)
    if agent_name not in agent_map:
        raise SystemExit(
            "agent %r not found in fleet %r (have: %s)"
            % (agent_name, fleet_name, sorted(agent_map))
        )
    target = agent_map[agent_name]
    audited_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    manifest = _ss.hermes_salvage_audit(agent_name, target, transport, audited_at=audited_at)
    _print(manifest)


def cmd_fleet_memory_export(args: argparse.Namespace) -> None:
    """Phase 2b: export the fleet's Qdrant vector memory to greppable JSONL for
    vetting (find stale facts that wouldn't surface from the soul text)."""
    import json as _json
    from mac import memory_vetting as _mv

    client = _mv.QdrantClient(args.qdrant_url)
    collections = _csv(args.collections) if args.collections else list(_mv.DEFAULT_COLLECTIONS)
    records = _mv.export_memory_records(client.scroll, collections, agent_id=args.agent or None)
    if args.search:
        records = _mv.search_records(records, args.search)
    if args.into:
        dest = Path(args.into).expanduser()
        dest.write_text("\n".join(_json.dumps(r, default=str) for r in records) + "\n", encoding="utf-8")
        _print({"qdrant": args.qdrant_url, "collections": collections, "records": len(records),
                "into": str(dest), "search": args.search})
    else:
        for r in records:
            sys.stdout.write(_json.dumps(r, default=str) + "\n")


def cmd_fleet_memory_prune(args: argparse.Namespace) -> None:
    """Phase 2b: delete vetted Qdrant point ids from a collection (destructive;
    operator-vetted). Ids come from --id (repeatable) or a JSONL export via
    --from-jsonl (uses each record's id)."""
    import json as _json
    from mac import memory_vetting as _mv

    ids: List[Any] = list(args.id or [])
    if args.from_jsonl:
        for line in Path(args.from_jsonl).expanduser().read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rec = _json.loads(line)
                if rec.get("id") is not None:
                    ids.append(rec["id"])
    client = _mv.QdrantClient(args.qdrant_url)
    _print(_mv.prune_points(client.delete, args.collection, ids))


def _hub_get_mood(agent_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """GET the agent's current mood overlay straight from the hub HTTP API.

    Fleet context is hub-backed on every node. Mood remains a small direct HTTP
    read because it is best-effort and not part of the dispatch facade. Returns
    the overlay dict, or None on any error / no active mood."""
    import json as _json
    import os as _os
    import urllib.request as _u

    from mac.fleet_env import resolve_first as _rt

    base = (_os.environ.get("MAC_HUB_URL") or _os.environ.get("MAC_URL") or "").rstrip("/")
    # Resolve fleet-aware so a legacy flat token can't shadow the scoped
    # MAC_*__<FLEET> form; fleet derives from MAC_FLEET (mac-g55y).
    token = (
        _rt(
            ["MAC_WORKER_TOKEN", "MAC_API_TOKEN"],
            fleet=_os.environ.get("MAC_FLEET"),
        )
        or ""
    ).strip()
    if not (base and token and agent_id):
        return None
    req = _u.Request("%s/agents/%s/mood" % (base, agent_id), method="GET")
    req.add_header("Authorization", "Bearer %s" % token)
    req.add_header("Accept", "application/json")
    try:
        with _u.urlopen(req, timeout=10) as resp:  # noqa: S310 (operator-configured hub)
            raw = resp.read().decode("utf-8", "replace")
        data = _json.loads(raw) if raw.strip() else None
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 - mood is best-effort
        return None


def cmd_fleet_refresh_context(args: argparse.Namespace) -> None:
    """fleet-02 + mood-01: refresh the live Fleet section AND this agent's mood
    overlay in its runtime-context markdown, so its next session knows what
    teammates are doing and actually behaves in its current mood. Idempotent."""
    import os as _os
    from pathlib import Path as _Path

    from mac.hermes_runtime import (
        refresh_fleet_section,
        refresh_mood_section,
        render_fleet_section,
        render_mood_section,
    )

    plane = _plane(args)
    agent = getattr(args, "agent", None)
    snapshot = plane.fleet_snapshot(exclude_agent_id=agent)
    markdown = getattr(args, "markdown", None) or _os.environ.get(
        "MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN"
    ) or str(mac_paths.gateway_home() / "mac-runtime-context.md")
    path = _Path(markdown)
    refresh_fleet_section(path, render_fleet_section(snapshot))

    # mood-01: fetch this agent's mood straight from the hub HTTP API. Best-effort.
    overlay = _hub_get_mood(agent) if agent else None
    refresh_mood_section(path, render_mood_section(overlay))
    _print(
        {
            "status": "refreshed",
            "markdown": markdown,
            "members": len(snapshot.get("members", [])),
            "mood": (overlay or {}).get("mode"),
        }
    )


def cmd_journal_snapshot(args: argparse.Namespace) -> None:
    """journal-01: snapshot this agent's soul + memory state into
    $HOME/.mac/journal/<date>/ and run the backup hook (unless --no-hook)."""
    from pathlib import Path as _Path

    from mac import journal as _journal

    root = _Path(args.dir).expanduser() if getattr(args, "dir", None) else None
    home = _Path(args.home).expanduser() if getattr(args, "home", None) else None
    m = _journal.snapshot(
        home=home,
        root=root,
        date=getattr(args, "date", None),
        agent_id=getattr(args, "agent", None),
        run_hook=not getattr(args, "no_hook", False),
    )
    _print(
        {
            "date": m["date"],
            "agent_id": m["agent_id"],
            "captured": m["captured"],
            "files": len(m["files"]),
            "path": str((root or _journal.journal_root()) / m["date"]),
            "hook": m.get("hook"),
        }
    )


def cmd_journal_list(args: argparse.Namespace) -> None:
    from pathlib import Path as _Path

    from mac import journal as _journal

    root = _Path(args.dir).expanduser() if getattr(args, "dir", None) else None
    _print(_journal.list_journals(root))


def cmd_journal_restore(args: argparse.Namespace) -> None:
    from pathlib import Path as _Path

    from mac import journal as _journal

    root = _Path(args.dir).expanduser() if getattr(args, "dir", None) else None
    home = _Path(args.home).expanduser() if getattr(args, "home", None) else None
    _print(
        _journal.restore(
            args.date, home=home, root=root, dry_run=getattr(args, "dry_run", False)
        )
    )


def _fleet_setup_plan_from_args(args: argparse.Namespace) -> Dict[str, Any]:
    from mac.fleet_setup import build_setup_plan, load_setup_spec, public_plan

    root = Path(__file__).resolve().parents[2]
    fleets_config = Path(args.fleets_config).expanduser()
    env_file = Path(args.env_file).expanduser()
    spec = load_setup_spec(Path(args.spec).expanduser())
    return public_plan(
        build_setup_plan(
            spec,
            root=root,
            fleets_config=fleets_config,
            env_file=env_file,
        )
    )


def cmd_fleet_validate_setup(args: argparse.Namespace) -> None:
    """Validate a declarative mac.fleet_setup.v1 spec."""
    _print(_fleet_setup_plan_from_args(args))


def cmd_fleet_doctor_setup(args: argparse.Namespace) -> None:
    """Run LLM-friendly setup doctor checks for a declarative fleet spec."""
    plan = _fleet_setup_plan_from_args(args)
    _print(
        {
            "schema": "mac.fleet_setup_doctor.v1",
            "status": plan.get("status"),
            "hub": plan.get("hub"),
            "fleet_name": plan.get("fleet_name"),
            "checks": plan.get("checks"),
            "required_env": plan.get("required_env"),
            "warnings": plan.get("warnings"),
            "errors": plan.get("errors"),
            "next_steps": plan.get("next_steps"),
        }
    )


def cmd_mood_set(args: argparse.Namespace) -> None:
    _print(
        _plane(args).set_mood(
            args.agent_id,
            args.mode,
            set_by=args.set_by,
            reason=args.reason,
            ttl_seconds=args.ttl_seconds,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_mood_show(args: argparse.Namespace) -> None:
    overlay = _plane(args).get_current_mood(args.agent_id)
    _print(overlay.to_dict() if overlay is not None else None)


def cmd_mood_clear(args: argparse.Namespace) -> None:
    cleared = _plane(args).clear_mood(
        args.agent_id, cleared_by=args.cleared_by, reason=args.reason
    )
    _print(cleared.to_dict() if cleared is not None else None)


def cmd_mood_history(args: argparse.Namespace) -> None:
    _print(
        [
            overlay.to_dict()
            for overlay in _plane(args).list_mood_history(args.agent_id, limit=args.limit)
        ]
    )


def cmd_nap_configure(args: argparse.Namespace) -> None:
    _print(
        _plane(args).configure_nap(
            args.agent_id,
            offset_minutes=args.offset_minutes,
            window_minutes=args.window_minutes,
            enabled=not args.disabled,
            actor=args.actor,
        )
    )


def cmd_nap_show(args: argparse.Namespace) -> None:
    schedule = _plane(args).get_nap_schedule(args.agent_id)
    _print(schedule.to_dict() if schedule is not None else None)


def cmd_nap_next(args: argparse.Namespace) -> None:
    _print(_plane(args).next_nap_window(args.agent_id))


def cmd_nap_begin(args: argparse.Namespace) -> None:
    _print(
        _plane(args).begin_nap(
            args.agent_id,
            actor=args.actor,
            detail=_json_arg(args.detail, {}),
        )
    )


def cmd_nap_complete(args: argparse.Namespace) -> None:
    _print(
        _plane(args).complete_nap(
            args.run_id,
            summary_evidence_id=args.evidence_id,
            detail=_json_arg(args.detail, None),
            actor=args.actor,
        )
    )


def cmd_nap_fail(args: argparse.Namespace) -> None:
    _print(_plane(args).fail_nap(args.run_id, args.reason, actor=args.actor))


def cmd_nap_list(args: argparse.Namespace) -> None:
    _print([run.to_dict() for run in _plane(args).list_nap_runs(args.agent_id)])


def cmd_dispatch_once(args: argparse.Namespace) -> None:
    _print(_plane(args).dispatch_once(args.lease_seconds))


def cmd_dispatch_tick(args: argparse.Namespace) -> None:
    _print(_plane(args).tick(args.lease_seconds, args.limit))


def cmd_message_send(args: argparse.Namespace) -> None:
    _print(
        _plane(args).send_message(
            sender_agent_id=args.sender_agent_id,
            recipient_agent_id=args.recipient_agent_id,
            message_type=args.message_type,
            payload=_json_arg(args.payload, {}),
            task_id=args.task_id,
        )
    )


def cmd_message_inbox(args: argparse.Namespace) -> None:
    _print([message.to_dict() for message in _plane(args).deliver_messages(args.agent_id, args.limit)])


def _agentbus_payload_arg(args: argparse.Namespace) -> Any:
    if args.payload is None:
        return None
    if args.payload_encoding == "json":
        return json.loads(args.payload)
    return args.payload


def cmd_agentbus_open(args: argparse.Namespace) -> None:
    _print(
        _plane(args).open_agentbus_stream(
            sender_agent_id=args.sender_agent_id,
            recipient_agent_id=args.recipient_agent_id,
            content_type=args.content_type,
            topic=args.topic,
            headers=_json_arg(args.headers, {}),
            task_id=args.task_id,
            stream_id=args.stream_id,
        )
    )


def cmd_agentbus_append(args: argparse.Namespace) -> None:
    _print(
        _plane(args).append_agentbus_chunk(
            args.stream_id,
            sender_agent_id=args.sender_agent_id,
            payload=_agentbus_payload_arg(args),
            content_type=args.content_type,
            payload_encoding=args.payload_encoding,
            final=args.final,
        )
    )


def cmd_agentbus_close(args: argparse.Namespace) -> None:
    _print(
        _plane(args).close_agentbus_stream(
            args.stream_id,
            sender_agent_id=args.sender_agent_id,
            status=args.status,
        )
    )


def cmd_agentbus_list(args: argparse.Namespace) -> None:
    _print(
        [
            stream.to_dict()
            for stream in _plane(args).list_agentbus_streams(
                agent_id=args.agent_id,
                status=args.status,
                limit=args.limit,
            )
        ]
    )


def cmd_agentbus_wait(args: argparse.Namespace) -> None:
    """Block until someone messages this agent, print what arrived, and exit.

    Designed to be run by a WORKING agent as a background task of its own
    harness. The agent starts this, keeps working in the foreground, and the
    harness surfaces the completion between steps -- so a correction reaches an
    agent mid-task instead of waiting for it to finish.

    It exits on the first batch rather than holding the channel open: a watcher
    exists to wake its caller, and the caller restarts it after acting. Always
    print `next_cursor`, including on timeout, so the restarted watcher resumes
    exactly where this one stopped and a message landing between rounds is not
    missed.
    """
    import time as _time

    cp = _plane(args)
    cursor = args.after_cursor or ""
    deadline = _time.monotonic() + max(1.0, float(args.timeout_seconds))
    interval = max(0.1, float(args.poll_interval_seconds))
    while True:
        chunks = cp.read_agentbus_inbox(args.agent_id, cursor, limit=args.limit)
        if chunks:
            cursor = cp.agentbus_inbox_cursor(chunks[-1])
            _print(
                {
                    "status": "message",
                    "count": len(chunks),
                    "next_cursor": cursor,
                    "messages": [chunk.to_dict() for chunk in chunks],
                }
            )
            return
        if _time.monotonic() >= deadline:
            _print({"status": "timeout", "count": 0, "next_cursor": cursor})
            return
        _time.sleep(interval)


def cmd_agentbus_read(args: argparse.Namespace) -> None:
    _print(
        [
            chunk.to_dict()
            for chunk in _plane(args).read_agentbus_chunks(
                args.agent_id,
                args.stream_id,
                after_sequence=args.after_sequence,
                limit=args.limit,
            )
        ]
    )


def cmd_agentbus_publish(args: argparse.Namespace) -> None:
    _print(
        _plane(args).publish_agentbus_content(
            sender_agent_id=args.sender_agent_id,
            recipient_agent_id=args.recipient_agent_id,
            content_type=args.content_type,
            payload=_agentbus_payload_arg(args),
            topic=args.topic,
            headers=_json_arg(args.headers, {}),
            task_id=args.task_id,
            payload_encoding=args.payload_encoding,
        )
    )


def cmd_agentbus_repo_update(args: argparse.Namespace) -> None:
    if not args.recipient_agent_id and not args.all_agents:
        raise MACError("repo-update requires --recipient-agent-id or --all-agents")
    _print(
        _plane(args).publish_agentbus_repo_update(
            sender_agent_id=args.sender_agent_id,
            recipient_agent_ids=args.recipient_agent_id,
            all_agents=args.all_agents,
            repo_path=args.repo_path,
            remote=args.remote,
            branch=args.branch,
            restart=not args.no_restart,
            restart_services=list(args.restart_service or []),
            request_id=args.request_id,
        )
    )


def cmd_agentbus_artifact_publish(args: argparse.Namespace) -> None:
    _print(
        _plane(args).publish_agentbus_artifact(
            sender_agent_id=args.sender_agent_id,
            operation=args.operation,
            recipient_agent_ids=list(args.recipient_agent_id or []),
            all_agents=args.all_agents,
            artifact_id=args.artifact,
            digest=args.digest,
            kind=args.kind,
            uri=args.uri,
            public_url=args.public_url,
            path=args.path,
            publish_dir=args.publish_dir,
            sbom_uri=args.sbom_uri,
            signers=list(_csv(args.signers)),
            metadata=_json_arg(args.metadata, {}),
            task_id=args.task_id,
            request_id=args.request_id,
        )
    )


def cmd_review_request(args: argparse.Namespace) -> None:
    _print(_plane(args).request_review(args.task_id, args.reviewer_agent_id, args.actor))


def cmd_review_decision(args: argparse.Namespace) -> None:
    _print(
        _plane(args).submit_review(
            args.review_id,
            status=args.status,
            reviewer_agent_id=args.reviewer_agent_id,
            reason=args.reason,
            evidence_id=args.evidence_id,
        )
    )


def cmd_review_experiment_assign(args: argparse.Namespace) -> None:
    hypothesis = _read_text_arg(
        args.hypothesis,
        args.hypothesis_file,
        label="review experiment hypothesis",
    ).strip()
    _print(
        _plane(args).assign_review_experiment(
            args.task_id,
            experiment_id=args.experiment_id,
            arm=args.arm,
            arms=_review_arm_weights(args.arms),
            assignment_probability=args.probability,
            blind=args.blind,
            blind_arms=args.blind_arm,
            policy_version=args.policy_version,
            hypothesis=hypothesis,
            stratum=args.stratum,
            actor=args.actor,
        )
    )


def cmd_review_experiment_observe(args: argparse.Namespace) -> None:
    _print(_plane(args).review_observation(args.task_id))


def cmd_review_experiment_outcome(args: argparse.Namespace) -> None:
    detail = _read_json_arg(
        args.detail,
        args.detail_file,
        label="review outcome detail",
        default={},
    )
    if not isinstance(detail, dict):
        raise MACError("review outcome detail must be a JSON object")
    _print(
        _plane(args).record_review_outcome(
            args.task_id,
            kind=args.kind,
            status=args.status,
            finding_id=args.finding_id,
            severity_weight=args.severity_weight,
            source=args.source,
            detail=detail,
            actor=args.actor,
        )
    )


def cmd_review_experiment_report(args: argparse.Namespace) -> None:
    _print(
        _plane(args).review_experiment_report(
            args.experiment_id,
            project=args.project,
            min_tasks_per_arm=args.min_tasks_per_arm,
            min_validated_outcomes_per_arm=args.min_validated_outcomes_per_arm,
        )
    )


def cmd_review_auto_land(args: argparse.Namespace) -> None:
    author = getattr(args, "author", "") or os.environ.get("MAC_AGENT_ID", "")
    if getattr(args, "dry_run", False):
        # Preview only: never runs the contract gate, spawns a reviewer, or lands.
        _print(
            {
                "schema": "mac.auto_land.dry_run.v1",
                "target": args.target,
                "repo_dir": args.repo_dir,
                "base_ref": args.base_ref,
                "push": bool(args.push),
                "author": author,
                "would_run": ["contract-gate", "adversarial-review"],
                "gates": [
                    "contract (scripts/run-contract-tests.sh)",
                    "adversarial-review (independent agent, default-to-reject)",
                    "independence (reviewer != author)",
                    "head_sha (land only the reviewed revision)",
                ],
                "note": "dry-run: no gate/review/land executed",
            }
        )
        return

    from mac.auto_land import build_real_dependencies, run_auto_land

    deps = build_real_dependencies(
        plane=_plane(args),
        repo_dir=args.repo_dir,
        base_ref=args.base_ref,
        created_by=args.created_by,
        allow_push=args.push,
        author=author,
    )
    decision = run_auto_land(args.target, **deps)
    _print(decision)


def cmd_publish(args: argparse.Namespace) -> None:
    _print(_plane(args).publish_task(args.task_id, args.target, args.created_by, evidence_id=args.evidence_id))


def cmd_pull_request_open(args: argparse.Namespace) -> None:
    from mac.gitops import open_pull_request

    repo_url = args.repo_url
    if not repo_url:
        raise SystemExit("--repo-url is required (or pipe the URL via $MAC_TASK_REPO_URL)")
    result = open_pull_request(
        repo_url,
        args.head,
        base=args.base,
        title=args.title,
        body=args.body,
    )
    output: Dict[str, Any] = {
        "host": result.host,
        "number": result.number,
        "url": result.url,
        "state": result.state,
        "head": args.head,
        "base": args.base or "(default)",
    }
    if args.task_id:
        try:
            finding = _plane(args).record_integration_finding(
                source_kind=result.host,
                source_id="%s#%d" % (result.url, result.number),
                finding_type="pull_request_opened",
                title="PR #%d opened for task %s" % (result.number, args.task_id),
                detail={
                    "task_id": args.task_id,
                    "pull_request_url": result.url,
                    "pull_request_number": result.number,
                    "head": args.head,
                    "base": args.base or "",
                    "repo_url": repo_url,
                },
                severity="info",
            )
            output["finding_id"] = finding.id if hasattr(finding, "id") else None
        except Exception as exc:  # noqa: BLE001
            output["finding_error"] = str(exc)
    _print(output)


def cmd_secret_set(args: argparse.Namespace) -> None:
    value = _resolve_secret_value(args)
    _print(_plane(args).create_secret(args.name, value, _json_arg(args.scopes, {}), args.created_by))


def _resolve_secret_value(args: argparse.Namespace) -> str:
    sources = [bool(args.value), bool(args.from_stdin), bool(args.from_file)]
    if sum(sources) != 1:
        raise MACError("exactly one of <value>, --from-stdin, --from-file is required")
    if args.from_stdin:
        return sys.stdin.read().rstrip("\n")
    if args.from_file:
        with open(args.from_file, "r", encoding="utf-8") as handle:
            return handle.read().rstrip("\n")
    return args.value


def cmd_secret_list(args: argparse.Namespace) -> None:
    _print([secret.to_dict() for secret in _plane(args).list_secrets()])


def cmd_secret_delete(args: argparse.Namespace) -> None:
    _print(_plane(args).delete_secret(args.secret, actor=args.actor))


def cmd_secret_rotate(args: argparse.Namespace) -> None:
    value = _resolve_secret_value(args)
    _print(_plane(args).rotate_secret(args.name, value, actor=args.actor))


def cmd_secret_access(args: argparse.Namespace) -> None:
    _print(_plane(args).request_secret(args.secret, args.agent_id, args.purpose))


def cmd_secret_audits(args: argparse.Namespace) -> None:
    _print([audit.to_dict() for audit in _plane(args).list_secret_audits(args.secret_id)])


def cmd_runtime_create(args: argparse.Namespace) -> None:
    _print(_plane(args).create_runtime(args.name, _json_arg(args.manifest, {}), args.created_by))


def cmd_runtime_list(args: argparse.Namespace) -> None:
    _print([runtime.to_dict() for runtime in _plane(args).list_runtimes()])


def cmd_runtime_delta_propose(args: argparse.Namespace) -> None:
    _print(
        _plane(args).propose_runtime_delta(
            args.task_id,
            args.agent_id,
            args.package_manager,
            _json_arg(args.commands, []),
            _json_arg(args.dependencies, []),
            args.reason,
            project=args.project,
            base_runtime_id=args.base_runtime,
            base_runtime_digest=args.base_runtime_digest,
            lockfile_path=args.lockfile_path,
            lockfile_digest=args.lockfile_digest,
            evidence_id=args.evidence_id,
        )
    )


def cmd_runtime_delta_list(args: argparse.Namespace) -> None:
    _print(
        [
            delta.to_dict()
            for delta in _plane(args).list_runtime_deltas(
                status=args.status,
                task_id=args.task_id,
                project=args.project,
                limit=args.limit,
            )
        ]
    )


def cmd_runtime_delta_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_runtime_delta(args.delta).to_dict())


def cmd_runtime_delta_validate(args: argparse.Namespace) -> None:
    _print(_plane(args).validate_runtime_delta(args.delta, args.actor).to_dict())


def cmd_runtime_delta_reject(args: argparse.Namespace) -> None:
    _print(_plane(args).reject_runtime_delta(args.delta, args.actor, args.reason).to_dict())


def cmd_runtime_delta_promote(args: argparse.Namespace) -> None:
    _print(
        _plane(args).promote_runtime_delta(
            args.delta,
            args.actor,
            runtime_name=args.runtime_name,
        ).to_dict()
    )


def cmd_artifact_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_artifact(
            kind=args.kind,
            digest=args.digest,
            uri=args.uri,
            created_by=args.created_by,
            sbom_uri=args.sbom_uri,
            signers=_csv(args.signers),
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_artifact_list(args: argparse.Namespace) -> None:
    _print([a.to_dict() for a in _plane(args).list_artifacts(args.kind)])


def cmd_artifact_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_artifact(args.artifact))


def cmd_artifact_delete(args: argparse.Namespace) -> None:
    _print(_plane(args).delete_artifact(args.artifact, actor=args.actor))


def cmd_migrate_import(args: argparse.Namespace) -> None:
    report = import_jsonl(_plane(args), path=Path(args.path))
    _print(report.to_dict())


def cmd_migrate_acc(args: argparse.Namespace) -> None:
    report = migrate_acc_sqlite(
        _plane(args),
        Path(args.acc_db),
        mode=args.mode,
        allow_active=args.allow_active,
        audit_limit=args.audit_limit,
        agent_home=Path(args.agent_home) if args.agent_home else None,
    ).to_dict()
    if args.report:
        Path(args.report).write_text(
            json.dumps(report, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    _print(report)


def cmd_env_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_environment(
            args.name,
            tenant_id=args.tenant_id,
            channel=args.channel,
            promotes_from=args.promotes_from,
            metadata=_json_arg(args.metadata, {}),
            created_by=args.created_by,
        )
    )


def cmd_env_list(args: argparse.Namespace) -> None:
    _print([e.to_dict() for e in _plane(args).list_environments(args.tenant_id, args.channel)])


def cmd_env_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_environment(args.environment))


def cmd_env_deploy(args: argparse.Namespace) -> None:
    _print(
        _plane(args).deploy_artifact(
            args.environment,
            args.artifact,
            args.actor,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_env_current(args: argparse.Namespace) -> None:
    current = _plane(args).current_deployment(args.environment)
    _print(current.to_dict() if current is not None else None)


def cmd_env_deployments(args: argparse.Namespace) -> None:
    _print([d.to_dict() for d in _plane(args).list_deployments(args.environment)])


def cmd_bridge_import(args: argparse.Namespace) -> None:
    _print(
        _plane(args).import_project_item(
            args.source,
            args.external_id,
            args.title,
            _json_arg(args.payload, {}),
            required_capabilities=_csv(args.required_capabilities),
            description=args.description,
            project=args.project,
            priority=args.priority,
            dependencies=_csv(args.dependencies),
            metadata=_json_arg(args.metadata, {}),
            actor=args.actor,
        )
    )


def cmd_bridge_list(args: argparse.Namespace) -> None:
    _print([item.to_dict() for item in _plane(args).list_project_items()])


def cmd_bridge_repository_register(args: argparse.Namespace) -> None:
    _print(
        _plane(args).register_project_repository(
            args.name,
            args.path,
            source=args.source,
            project=args.project,
            required_capabilities=_csv(args.required_capabilities),
            enabled=not args.disabled,
            poll_interval_seconds=args.poll_interval_seconds,
            metadata=_json_arg(args.metadata, {}),
            actor=args.actor,
        )
    )


def cmd_bridge_repository_list(args: argparse.Namespace) -> None:
    _print(
        [
            repo.to_dict()
            for repo in _plane(args).list_project_repositories(enabled=args.enabled)
        ]
    )


def cmd_integrations_findings(args: argparse.Namespace) -> None:
    _print(
        [
            finding.to_dict()
            for finding in _plane(args).list_integration_findings(
                source_kind=args.source_kind,
                source_id=args.source_id,
                finding_type=args.finding_type,
                status=args.status,
                severity=args.severity,
                limit=args.limit,
            )
        ]
    )


def cmd_integrations_observations(args: argparse.Namespace) -> None:
    _print(
        [
            observation.to_dict()
            for observation in _plane(args).list_integration_observations(
                source_kind=args.source_kind,
                source_id=args.source_id,
                authority=args.authority,
                status=args.status,
                limit=args.limit,
            )
        ]
    )


def cmd_memory_add(args: argparse.Namespace) -> None:
    _print(
        _plane(args).add_memory(
            task_id=args.task_id,
            subject_type=args.subject_type,
            subject_id=args.subject_id,
            record_type=args.record_type,
            content=args.content,
            evidence_id=args.evidence_id,
            created_by=args.created_by,
        )
    )


def cmd_memory_search(args: argparse.Namespace) -> None:
    _print(
        [
            record.to_dict()
            for record in _plane(args).search_memory(
                task_id=args.task_id,
                subject_type=args.subject_type,
                subject_id=args.subject_id,
                record_type=getattr(args, "record_type", None),
                record_type_prefix=getattr(args, "record_type_prefix", None),
                created_by=getattr(args, "created_by", None),
                since=getattr(args, "since", None),
                until=getattr(args, "until", None),
                limit=getattr(args, "limit", None),
                order=getattr(args, "order", "asc"),
            )
        ]
    )


def cmd_memory_remember(args: argparse.Namespace) -> None:
    """`bd remember` equivalent — store an ambient project-scoped fact
    keyed by name. Subsequent calls with the same key overwrite."""
    _print(
        _plane(args).remember_memory(
            args.key,
            args.content,
            project=args.project,
            actor=args.actor,
        )
    )


def _build_vector_writer(args: argparse.Namespace):
    """Construct a VectorWriterService for CLI commands. The Qdrant
    endpoint defaults to the same Qdrant env cascade the hub uses, then
    http://127.0.0.1:6333. Tests inject the writer directly; this
    builder is for operator use.
    """
    import os

    from mac.vector_writer_service import VectorWriterService

    cp = _plane(args)
    qdrant_url = (
        getattr(args, "qdrant_url", None)
        or os.environ.get("MAC_QDRANT_URL")
        or os.environ.get("QDRANT_URL")
        or os.environ.get("QDRANT_ADDRESS")
        or os.environ.get("QDRANT_FLEET_URL")
        or "http://127.0.0.1:6333"
    )
    return VectorWriterService(memory=cp.memory, qdrant_url=qdrant_url)


def cmd_memory_embed(args: argparse.Namespace) -> None:
    """mem-07: embed one memory_record into the medium tier."""
    writer = _build_vector_writer(args)
    ref = writer.embed_memory(args.memory_id, tier=args.tier)
    _print(ref.to_dict())


def cmd_memory_backfill(args: argparse.Namespace) -> None:
    """mem-07: embed every memory_record that isn't already in the tier."""
    writer = _build_vector_writer(args)
    _print(writer.backfill(tier=args.tier, limit=args.limit))


def cmd_memory_health(args: argparse.Namespace) -> None:
    """mem-10: memory-tier health snapshot."""
    cp = _plane(args)
    kwargs: Dict[str, Any] = {"nap_interval_hours": args.nap_interval_hours}
    qdrant_url = getattr(args, "qdrant_url", None)
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.memory_health(**kwargs))


def cmd_memory_recall(args: argparse.Namespace) -> None:
    """mem-09: vector-tier recall, hub-routable when MAC_API_URL is set."""
    cp = _plane(args)
    # If the dispatch is local (operator running `mac --db ...`), we
    # need to provide a Qdrant URL; if it's remote, the HTTP route
    # already resolves Qdrant on the hub side.
    qdrant_url = getattr(args, "qdrant_url", None)
    kwargs = {
        "tier": args.tier,
        "limit": args.limit,
        "min_score": args.min_score,
        "project": args.project,
        "tenant_id": args.tenant_id,
    }
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.recall_memory(args.query, **kwargs))


def cmd_memory_recall_dreams(args: argparse.Namespace) -> None:
    """Recall typed dream artifacts using scope/kind/confidence filters."""
    cp = _plane(args)
    qdrant_url = getattr(args, "qdrant_url", None)
    kwargs = {
        "tier": args.tier,
        "limit": args.limit,
        "min_score": args.min_score,
        "project": args.project,
        "agent_id": args.agent_id,
        "scope": args.scope,
        "kind": args.kind,
        "min_confidence": args.min_confidence,
        "tenant_id": args.tenant_id,
    }
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.recall_dream_artifacts(args.query, **kwargs))


def cmd_dream_run(args: argparse.Namespace) -> None:
    """Curate memory into a reviewable candidate store.

    Writes nothing to live memory: the run lands in dream_runs for review, and
    ``mac admin dream promote`` adopts it. Use --promote to skip review when every
    gate passes."""
    from mac.dreaming import DreamPolicy

    cp = _plane(args)
    policy = DreamPolicy(
        instructions=getattr(args, "instructions", "") or "",
        max_output_ratio=float(getattr(args, "max_output_ratio", 0.75)),
    )
    _print(
        cp.run_dream_cycle(
            agent_id=getattr(args, "agent_id", None),
            project=getattr(args, "project", None),
            since=getattr(args, "since", "") or "",
            limit=int(getattr(args, "limit", 2000)),
            policy=policy,
            auto_promote=bool(getattr(args, "promote", False)),
        )
    )


def cmd_dream_list(args: argparse.Namespace) -> None:
    """List dream runs, newest first."""
    cp = _plane(args)
    _print(cp.list_dream_runs(state=getattr(args, "state", None), limit=int(args.limit)))


def cmd_dream_show(args: argparse.Namespace) -> None:
    """Show one dream run: its gates, stats and candidate memories."""
    cp = _plane(args)
    _print(cp.get_dream_run(args.run_id))


def cmd_dream_promote(args: argparse.Namespace) -> None:
    """Adopt a reviewed run into live memory, retiring what it supersedes."""
    cp = _plane(args)
    _print(
        cp.promote_dream_run(
            args.run_id,
            retire_superseded=not bool(getattr(args, "keep_superseded", False)),
        )
    )


def cmd_dream_discard(args: argparse.Namespace) -> None:
    """Discard a dream run. Its candidates stay readable for inspection."""
    cp = _plane(args)
    _print(cp.discard_dream_run(args.run_id, reason=getattr(args, "reason", "")))


def cmd_dream_import_logs(args: argparse.Namespace) -> None:
    """Merge the gateway's orphaned ~/.hermes/dream_logs reports into durable
    memory (record_type dream:imported_report), embedding each so it is
    retrievable via dream recall. Idempotent; dedups on findings."""
    cp = _plane(args)
    kwargs = {
        "dream_logs_dir": getattr(args, "dream_logs_dir", None),
        "agent_id": getattr(args, "agent_id", None),
        "embed": not args.no_embed,
        "dry_run": args.dry_run,
    }
    qdrant_url = getattr(args, "qdrant_url", None)
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.import_dream_logs(**kwargs))


def cmd_nap_cycle(args: argparse.Namespace) -> None:
    """mem-08 autonomy: begin + consolidate + complete in one shot."""
    from mac.dispatch import RemoteDispatch

    cp = _plane(args)
    qdrant_url = getattr(args, "qdrant_url", None)
    writer = None
    if not args.no_embed and not isinstance(cp, RemoteDispatch):
        writer = _build_vector_writer(args)
    kwargs = {
        "actor": args.actor,
        "vector_writer": writer,
        "embed_into_medium": not args.no_embed,
        "emit_dream_artifacts": not args.no_dreams,
    }
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.run_nap_cycle(args.agent_id, **kwargs))


def cmd_nap_due(args: argparse.Namespace) -> None:
    """List agents whose nap window has opened and hasn't been completed."""
    due = _plane(args).list_due_nap_agents(as_of=args.as_of)
    if getattr(args, "format", "json") == "agent-ids":
        # Newline-delimited agent_ids for `xargs` in the nap-tick unit —
        # avoids piping JSON through an embedded interpreter.
        for item in due:
            print(item["agent_id"])
        return
    _print(due)


def cmd_nap_consolidate(args: argparse.Namespace) -> None:
    """mem-08: walk the agent's recent memory_records, write per-group
    summaries, and embed them into the medium tier."""
    from mac.dispatch import RemoteDispatch

    cp = _plane(args)
    qdrant_url = getattr(args, "qdrant_url", None)
    writer = None
    if not args.no_embed and not isinstance(cp, RemoteDispatch):
        writer = _build_vector_writer(args)
    kwargs = {
        "since": args.since,
        "nap_run_id": args.nap_run_id,
        "embed_into_medium": not args.no_embed,
        "emit_dream_artifacts": not args.no_dreams,
        "vector_writer": writer,
        "created_by": args.created_by,
    }
    if qdrant_url:
        kwargs["qdrant_url"] = qdrant_url
    _print(cp.consolidate_nap(args.agent_id, **kwargs))


def cmd_memory_list(args: argparse.Namespace) -> None:
    _print(
        [
            item.to_dict() if hasattr(item, "to_dict") else item
            for item in _plane(args).list_remembered_memory(project=args.project)
        ]
    )


def cmd_memory_forget(args: argparse.Namespace) -> None:
    _print(_plane(args).forget_memory(args.key, project=args.project))


def cmd_curiosity_list(args: argparse.Namespace) -> None:
    """List curiosity candidates through the hub.

    The ledger lives inside the owning agent's OpenClaw sandbox, which a task
    sandbox cannot reach, so reading it locally only works on the host. Going
    through the hub works from anywhere, including a dispatched task
    (task_3a4503f0).
    """
    _print(_plane(args).list_curiosity_candidates(args.status))


def cmd_curiosity_decide(args: argparse.Namespace) -> None:
    """Approve or reject one candidate, with the audit trail the ledger wants."""
    _print(
        _plane(args).decide_curiosity_candidate(
            args.candidate_id,
            args.decision,
            actor=args.actor,
            reason=args.reason,
            approval_id=args.approval_id,
        )
    )


def cmd_memory_decay(args: argparse.Namespace) -> None:
    """dream-04: forget stale, low-salience memory (dry-run unless --apply)."""
    _print(
        _plane(args).decay_memory(
            ttl_days=args.ttl_days,
            dry_run=not args.apply,
            limit=args.limit,
        )
    )


def cmd_rollout_create(args: argparse.Namespace) -> None:
    _print(
        _plane(args).create_rollout(
            args.version,
            args.strategy,
            args.target_percent,
            args.created_by,
            tenant_id=args.tenant_id,
            channel=args.channel,
            runtime_environment_id=args.runtime,
            artifact_uri=args.artifact_uri,
            artifact_hash=args.artifact_hash,
            health_policy=_json_arg(args.health_policy, {}),
            required_eval_set_id=args.required_eval_set_id,
        )
    )


def cmd_eval_set_create(args: argparse.Namespace) -> None:
    _print(
        _plane(args).create_eval_set(
            args.name,
            scoring=args.scoring,
            description=args.description or "",
            baseline_score=args.baseline_score,
            regression_threshold=args.regression_threshold,
            metadata=_json_arg(args.metadata, {}),
            created_by=args.created_by,
        )
    )


def cmd_eval_set_list(args: argparse.Namespace) -> None:
    _print([eval_set.to_dict() for eval_set in _plane(args).list_eval_sets()])


def cmd_eval_set_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_eval_set(args.eval_set))


def cmd_eval_set_baseline(args: argparse.Namespace) -> None:
    _print(_plane(args).update_eval_set_baseline(args.eval_set, args.baseline_score, args.actor))


def cmd_eval_run_record(args: argparse.Namespace) -> None:
    _print(
        _plane(args).record_eval_run(
            args.eval_set,
            args.target_kind,
            args.target_id,
            args.score,
            detail=_json_arg(args.detail, {}),
            evidence_id=args.evidence_id,
            created_by=args.created_by,
        )
    )


def cmd_eval_run_list(args: argparse.Namespace) -> None:
    _print([run.to_dict() for run in _plane(args).list_eval_runs(args.eval_set, args.target_id)])


def cmd_events_list(args: argparse.Namespace) -> None:
    _print(
        _plane(args).list_events(
            subject_type=args.subject_type,
            subject_id=args.subject_id,
            actor=args.actor,
            event_type=args.event_type,
            event_type_prefix=args.prefix,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    )


def cmd_action_events_list(args: argparse.Namespace) -> None:
    _print(
        [
            event.to_dict() if hasattr(event, "to_dict") else event
            for event in _plane(args).list_action_events(
                agent_id=args.agent_id,
                task_id=args.task_id,
                session_id=args.session_id,
                sandbox_id=args.sandbox_id,
                policy_id=args.policy_id,
                action_type=args.action_type,
                outcome=args.outcome,
                since=args.since,
                until=args.until,
                limit=args.limit,
            )
        ]
    )


def cmd_action_events_stream(args: argparse.Namespace) -> None:
    import time as _time

    cursor = args.since
    deadline = None if args.follow else _time.monotonic() + max(0.0, float(args.timeout))
    while True:
        events = list(
            _plane(args).list_action_events(
                agent_id=args.agent_id,
                task_id=args.task_id,
                session_id=args.session_id,
                sandbox_id=args.sandbox_id,
                policy_id=args.policy_id,
                action_type=args.action_type,
                outcome=args.outcome,
                since=cursor,
                limit=args.limit,
            )
        )
        if events:
            for event in reversed(events):
                payload = event.to_dict() if hasattr(event, "to_dict") else event
                print(json.dumps(payload, sort_keys=True))
                cursor = payload.get("timestamp")
            sys.stdout.flush()
        if not args.follow and (deadline is None or _time.monotonic() >= deadline):
            break
        _time.sleep(max(0.25, float(args.interval)))


def cmd_action_events_export_otlp(args: argparse.Namespace) -> None:
    _print(
        _plane(args).export_action_events_otlp(
            agent_id=args.agent_id,
            task_id=args.task_id,
            session_id=args.session_id,
            sandbox_id=args.sandbox_id,
            policy_id=args.policy_id,
            action_type=args.action_type,
            outcome=args.outcome,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    )


def cmd_command_audit_list(args: argparse.Namespace) -> None:
    _print(
        [
            record.to_dict()
            for record in _plane(args).list_command_audit(
                agent_id=args.agent_id,
                task_id=args.task_id,
                command_id=args.command_id,
                phase=args.phase,
                since=args.since,
                until=args.until,
                limit=args.limit,
            )
        ]
    )


def cmd_observability_list(args: argparse.Namespace) -> None:
    _print(
        [
            event.to_dict()
            for event in _plane(args).list_observability(
                kind=args.kind,
                layer=args.layer,
                level=args.level,
                name=args.name,
                subject_type=args.subject_type,
                subject_id=args.subject_id,
                since=args.since,
                until=args.until,
                after_sequence=args.after_sequence,
                limit=args.limit,
            )
        ]
    )


def cmd_observability_prune(args: argparse.Namespace) -> None:
    removed = _plane(args).prune_observability(
        older_than=args.older_than,
        keep_last=args.keep_last,
    )
    _print({"removed": removed})


def cmd_memory_summarize_actions(args: argparse.Namespace) -> None:
    _print(
        _plane(args).summarize_actions_to_memory(
            agent_id=args.agent,
            since=args.since,
            created_by=args.created_by,
            write=not args.dry_run,
        )
    )


def cmd_workflow_decisions(args: argparse.Namespace) -> None:
    """wf-02: list every human-decision gate in a workflow or live run."""
    cp = _plane(args)
    target = args.id_or_slug
    if target.startswith("run_"):
        _print(cp.workflow_run_decisions(target))
    else:
        _print(cp.workflow_decisions(target, tenant_id=args.tenant_id))


def cmd_workflow_start(args: argparse.Namespace) -> None:
    """wf-03: start a workflow run, optionally with pre-supplied approval
    decisions so the run can advance through approval gates unattended."""
    cp = _plane(args)
    pre_decisions: Dict[str, str] = {}
    for spec in args.pre_decision or []:
        if "=" not in spec:
            raise MACError(
                "--pre-decision expects <node_key>=approved|rejected (got %r)" % spec
            )
        key, _, value = spec.partition("=")
        pre_decisions[key.strip()] = value.strip().lower()
    input_obj = _json_arg(args.input, {})
    _print(
        cp.start_workflow(
            args.workflow_id_or_slug,
            started_by=args.started_by,
            input=input_obj,
            tenant_id=args.tenant_id,
            pre_decisions=pre_decisions or None,
        )
    )


def cmd_notifier_configure(args: argparse.Namespace) -> None:
    _print(
        _plane(args).configure_notifier_channel(
            args.name,
            args.channel_type,
            event_types=_csv(args.event_types),
            target=_json_arg(args.target, {}),
            metadata=_json_arg(args.metadata, {}),
            enabled=not args.disabled,
        )
    )


def cmd_notifier_list(args: argparse.Namespace) -> None:
    _print(
        [
            channel.to_dict()
            for channel in _plane(args).list_notifier_channels(
                enabled=args.enabled,
                channel_type=args.channel_type,
            )
        ]
    )


def cmd_notifier_delete(args: argparse.Namespace) -> None:
    _plane(args).delete_notifier_channel(args.channel_id_or_name)
    _print({"deleted": args.channel_id_or_name})


def cmd_notifier_deliver(args: argparse.Namespace) -> None:
    _print(
        _plane(args).deliver_pending_notifications(
            limit=args.limit,
            notification_id=args.notification_id,
        )
    )


def cmd_communication_identity_configure(args: argparse.Namespace) -> None:
    _print(
        _plane(args).configure_communication_identity(
            args.name,
            display_name=args.display_name or "",
            description=args.description or "",
            is_default=args.default,
            enabled=not args.disabled,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_communication_identity_list(args: argparse.Namespace) -> None:
    _print(
        [
            item.to_dict()
            for item in _plane(args).list_communication_identities(args.enabled)
        ]
    )


def cmd_communication_identity_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_communication_identity(args.identity))


def cmd_communication_identity_delete(args: argparse.Namespace) -> None:
    _plane(args).delete_communication_identity(args.identity)
    _print({"deleted": args.identity})


def cmd_communication_account_configure(args: argparse.Namespace) -> None:
    _print(
        _plane(args).configure_communication_account(
            args.identity,
            args.channel,
            account_id=args.account_id,
            credential_refs=_json_arg(args.credential_refs, {}),
            config=_json_arg(args.config, {}),
            enabled=not args.disabled,
        )
    )


def cmd_communication_account_list(args: argparse.Namespace) -> None:
    _print(
        [
            item.to_dict()
            for item in _plane(args).list_communication_accounts(
                identity_id=args.identity,
                channel=args.channel,
                enabled=args.enabled,
            )
        ]
    )


def cmd_communication_account_show(args: argparse.Namespace) -> None:
    _print(_plane(args).get_communication_account(args.account))


def cmd_communication_account_delete(args: argparse.Namespace) -> None:
    _plane(args).delete_communication_account(args.account)
    _print({"deleted": args.account})


def cmd_communication_representation_configure(args: argparse.Namespace) -> None:
    _print(
        _plane(args).configure_representation_binding(
            args.subject_kind,
            args.subject_id,
            identity_id=args.identity,
            mode=args.mode,
            priority=args.priority,
            enabled=not args.disabled,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_communication_representation_list(args: argparse.Namespace) -> None:
    _print(
        [
            item.to_dict()
            for item in _plane(args).list_representation_bindings(
                subject_kind=args.subject_kind,
                identity_id=args.identity,
                enabled=args.enabled,
            )
        ]
    )


def cmd_communication_representation_resolve(args: argparse.Namespace) -> None:
    _print(
        _plane(args).resolve_agent_representation(
            args.agent_id,
            project=args.project,
            role=args.role,
            fleet=args.representation_fleet,
        )
    )


def cmd_communication_representation_delete(args: argparse.Namespace) -> None:
    _plane(args).delete_representation_binding(args.binding_id)
    _print({"deleted": args.binding_id})


def cmd_communication_lease_acquire(args: argparse.Namespace) -> None:
    _print(
        _plane(args).acquire_gateway_identity_lease(
            args.account_id,
            args.agent_id,
            lease_seconds=args.lease_seconds,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_communication_lease_list(args: argparse.Namespace) -> None:
    _print(
        [
            item.to_dict()
            for item in _plane(args).list_gateway_identity_leases(
                agent_id=args.agent_id, active_only=args.active_only
            )
        ]
    )


def cmd_communication_lease_renew(args: argparse.Namespace) -> None:
    _print(
        _plane(args).renew_gateway_identity_lease(
            args.lease_id,
            args.agent_id,
            args.fencing_token,
            lease_seconds=args.lease_seconds,
        )
    )


def cmd_communication_lease_release(args: argparse.Namespace) -> None:
    _plane(args).release_gateway_identity_lease(
        args.lease_id, args.agent_id, args.fencing_token
    )
    _print({"released": args.lease_id})


def cmd_communication_send(args: argparse.Namespace) -> None:
    body = args.body
    if args.body_file:
        body = sys.stdin.read() if args.body_file == "-" else Path(args.body_file).read_text()
    _print(
        _plane(args).enqueue_human_message(
            args.target,
            body,
            origin_agent_id=args.origin_agent_id,
            identity_id=args.identity,
            account_id=args.account_id,
            channel=args.channel,
            task_id=args.task_id,
            idempotency_key=args.idempotency_key,
            max_attempts=args.max_attempts,
            metadata=_json_arg(args.metadata, {}),
        )
    )


def cmd_communication_deliveries(args: argparse.Namespace) -> None:
    _print(
        [
            item.to_dict()
            for item in _plane(args).list_human_messages(
                status=args.status,
                identity_id=args.identity,
                origin_agent_id=args.origin_agent_id,
                limit=args.limit,
            )
        ]
    )


def cmd_rollout_list(args: argparse.Namespace) -> None:
    _print([rollout.to_dict() for rollout in _plane(args).list_rollouts(args.tenant_id, args.channel)])


def cmd_rollout_advance(args: argparse.Namespace) -> None:
    _print(_plane(args).advance_rollout(args.rollout_id, args.action, args.actor, _json_arg(args.detail, {})))


def cmd_rollout_rescue(args: argparse.Namespace) -> None:
    rollout, task = _plane(args).rescue_rollout(
        args.rollout_id,
        args.actor,
        args.reason,
        _json_arg(args.detail, {}),
    )
    _print({"rollout": rollout.to_dict(), "task": task.to_dict()})


def cmd_rollout_verify_artifact(args: argparse.Namespace) -> None:
    _print(
        _plane(args).verify_rollout_artifact(
            args.rollout_id,
            args.artifact_uri,
            args.artifact_hash,
            args.actor,
        )
    )


def cmd_rollout_health(args: argparse.Namespace) -> None:
    _print(
        _plane(args).evaluate_rollout_health(
            args.rollout_id,
            _json_arg(args.checks, {}),
            args.actor,
        )
    )


def cmd_plan_order(args: argparse.Namespace) -> None:
    """Handler for ``mac admin plan order <paths...>``."""
    from mac.planning import order_layers

    repo = getattr(args, "repo", None) or "."
    mode = "core-first" if getattr(args, "core_first", False) else "leaf-first"
    paths: List[str] = list(args.paths or [])

    result = order_layers(paths, repo_root=repo, mode=mode)
    _print(result.to_dict())


def _hgx_registered_agents(args: argparse.Namespace) -> Any:
    """Load the immutable-session -> registered-agent map for reconciliation.

    Accepts a JSON object (``{session_id: agent_id}``) or a JSON array of
    ``{"session_id": ..., "agent_id": ...}`` records so live capacity planning
    can count already-onboarded HGX sessions instead of treating them as spare
    quota. Returns ``None`` when the operator supplies no registry snapshot.
    """

    path = getattr(args, "registered_agents_file", None)
    if not path:
        return None
    import json as _json
    from pathlib import Path as _Path

    try:
        payload = _json.loads(_Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit("unable to read registered-agents file: %s" % exc)
    return payload


def _hgx_capacity_controller(args: argparse.Namespace) -> Any:
    from mac.hgx_elastic_capacity import (
        HgxCapacityPolicy,
        HgxElasticCapacityController,
    )
    from mac.hgx_provider import HgxProvider

    policy = HgxCapacityPolicy(
        min_ready=args.min_ready,
        max_sessions=args.max_sessions,
        headroom=args.headroom,
        cluster=args.cluster,
        gpu_count=args.gpu,
        memory_gib=args.memory_gib,
        cpu_count=args.cpu,
        max_create_per_run=args.max_create_per_run,
        cooldown_seconds=args.cooldown_seconds,
        wait_timeout_seconds=args.wait_timeout_seconds,
        poll_interval_seconds=args.poll_interval_seconds,
    )
    provider = HgxProvider(
        binary=args.hgx_binary,
        timeout=args.hgx_command_timeout_seconds,
    )
    return HgxElasticCapacityController(
        provider=provider,
        policy=policy,
        state_path=args.state_file,
        name_prefix=args.name_prefix,
    )


def cmd_hgx_capacity_status(args: argparse.Namespace) -> None:
    """Inspect HGX capacity without mutating provider or controller state."""

    _print(
        _hgx_capacity_controller(args).status(
            pending_request_count=args.pending_requests,
            registered_agents=_hgx_registered_agents(args),
        )
    )


def cmd_hgx_capacity_plan(args: argparse.Namespace) -> None:
    """Plan bounded HGX capacity changes without applying them."""

    _print(
        _hgx_capacity_controller(args).plan(
            pending_request_count=args.pending_requests,
            registered_agents=_hgx_registered_agents(args),
        )
    )


def cmd_hgx_capacity_execute(args: argparse.Namespace) -> None:
    """Explicitly create and nonce-attest bounded standard-dind capacity."""

    _print(
        _hgx_capacity_controller(args).execute(
            pending_request_count=args.pending_requests,
            registered_agents=_hgx_registered_agents(args),
        )
    )


def cmd_hgx_capacity_mark_onboarded(args: argparse.Namespace) -> None:
    """Consume an attested capacity receipt after real agent registration."""

    from mac.hgx_elastic_capacity import HgxElasticCapacityController

    _print(
        HgxElasticCapacityController(
            state_path=args.state_file
        ).mark_onboarded(
            args.session_id,
            agent_id=args.agent_id,
        )
    )


def _add_hgx_capacity_args(parser: argparse.ArgumentParser) -> None:
    from mac.hgx_elastic_capacity import DEFAULT_STATE_PATH

    parser.add_argument(
        "--pending-requests",
        type=int,
        default=0,
        help=(
            "durable pending ProvisioningService request count; the controller "
            "does not mark those requests fulfilled until a MAC agent exists"
        ),
    )
    parser.add_argument("--min-ready", type=int, default=0)
    parser.add_argument("--max-sessions", type=int, default=10)
    parser.add_argument("--headroom", type=int, default=0)
    parser.add_argument(
        "--cluster",
        default="gke-newhouse",
        help="explicit HGX cluster (default: gke-newhouse)",
    )
    parser.add_argument(
        "--gpu",
        type=int,
        default=1,
        help="GPU count, bounded to 0..8 (default: 1)",
    )
    parser.add_argument(
        "--memory-gib",
        type=int,
        default=64,
        help="memory request in GiB, bounded to 8..256 (default: 64)",
    )
    parser.add_argument(
        "--cpu",
        type=int,
        default=8,
        help="CPU request, bounded to 1..64 (default: 8)",
    )
    parser.add_argument(
        "--max-create-per-run",
        type=int,
        default=1,
        help="maximum sessions created by one execute invocation (default: 1)",
    )
    parser.add_argument("--cooldown-seconds", type=float, default=300.0)
    parser.add_argument("--wait-timeout-seconds", type=float, default=300.0)
    parser.add_argument("--poll-interval-seconds", type=float, default=5.0)
    parser.add_argument(
        "--state-file",
        default=DEFAULT_STATE_PATH,
        help="durable controller receipt path (written only by execute)",
    )
    parser.add_argument(
        "--registered-agents-file",
        default=None,
        help=(
            "JSON map or record list binding immutable HGX session IDs to "
            "registered fungible agents; already-onboarded healthy sessions are "
            "counted as capacity instead of being treated as spare quota"
        ),
    )
    parser.add_argument("--name-prefix", default="mac-fungible")
    parser.add_argument("--hgx-binary", default="hgx")
    parser.add_argument(
        "--hgx-command-timeout-seconds",
        type=float,
        default=120.0,
    )


def _set(func: Callable[[argparse.Namespace], None], parser: argparse.ArgumentParser) -> None:
    parser.set_defaults(func=func)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mac", description="Multi-agent coordinator control plane")
    # Transport selection (see mac.dispatch.resolve_dispatch for resolution).
    # --db is no longer auto-defaulted: the CLI either targets a hub (default
    # when MAC_API_URL or fleets.yaml is configured) or an explicit PostgreSQL
    # DSN. Silent fallback to ./mac.db is gone.
    parser.add_argument(
        "--db",
        default=None,
        help="direct PostgreSQL control-plane authority (a postgres:// DSN) for "
        "hub maintenance, standalone development, tests, and migration. It is "
        "not a repository ticket store or offline hub replica and never "
        "synchronizes with a hub. When unset and no hub is configured, mac "
        "refuses to run.",
    )
    parser.add_argument(
        "--local-authority",
        action="store_true",
        help="enable stopped-hub maintenance against the authoritative "
        "PostgreSQL database selected by --db or MAC_DB. The command refuses "
        "this mode while the configured hub health endpoint is reachable.",
    )
    parser.add_argument(
        "--hub-url",
        default=None,
        help="MAC hub URL (hub mode). Falls back to $MAC_API_URL / "
        "$MAC_URL / $MAC_HUB_URL, then ~/.mac/fleets.yaml for --fleet.",
    )
    parser.add_argument(
        "--token",
        default=None,
        help="Bearer token for hub mode. Falls back to $MAC_API_TOKEN "
        "(or $MAC_API_TOKEN__<FLEET> when --fleet is set).",
    )
    parser.add_argument(
        "--fleet",
        default=None,
        help="Fleet name; selects MAC_API_TOKEN__<FLEET> and "
        "~/.mac/fleets.yaml entry.",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="Secure client profile under ~/.mac/clients. Falls back to "
        "$MAC_PROFILE or the active profile.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit JSON instead of the default human-readable text. Works in any "
        "position (e.g. `mac task list --json` or `mac --json task list`).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    diagnostics_parser = sub.add_parser(
        "diagnostics", help="run read-only control-plane health checks"
    )
    diagnostics_parser.add_argument(
        "--check", action="append", help="run only the named check (repeatable)"
    )
    _set(cmd_diagnostics, diagnostics_parser)

    _set(
        cmd_init,
        sub.add_parser(
            "init", help="create the control-plane schema in the --db PostgreSQL store"
        ),
    )

    database = sub.add_parser(
        "database",
        help="offline durable-authority maintenance and migration",
    ).add_subparsers(dest="database_command", required=True)
    sqlite_to_postgres = database.add_parser(
        "migrate-sqlite-to-postgres",
        help="copy every SQLite row to PostgreSQL and verify full-row digests",
    )
    sqlite_to_postgres.add_argument(
        "--sqlite",
        required=True,
        help="stopped hub's authoritative SQLite database",
    )
    target = sqlite_to_postgres.add_mutually_exclusive_group()
    target.add_argument(
        "--postgres-url",
        help="PostgreSQL DSN; prefer an environment variable/file for secret DSNs",
    )
    target.add_argument(
        "--postgres-url-file",
        help="owner-readable file containing only the PostgreSQL DSN",
    )
    sqlite_to_postgres.add_argument(
        "--report",
        help="durable resumable receipt (default: <sqlite>.postgres.json)",
    )
    sqlite_to_postgres.add_argument(
        "--batch-size",
        type=int,
        default=10_000,
        help="COPY fetch batch size, 1..100000 (default: 10000)",
    )
    sqlite_to_postgres.add_argument(
        "--hub-stopped",
        action="store_true",
        help="assert every process capable of writing the SQLite authority is stopped",
    )
    mode = sqlite_to_postgres.add_mutually_exclusive_group()
    mode.add_argument(
        "--resume",
        action="store_true",
        help="resume only tables proved by the existing receipt",
    )
    mode.add_argument(
        "--restart",
        action="store_true",
        help="destructively clear the target and restart this migration",
    )
    mode.add_argument(
        "--verify-only",
        action="store_true",
        help="re-verify every receipted table without copying",
    )
    _set(cmd_database_migrate_sqlite_postgres, sqlite_to_postgres)

    # mac admin config migrate-env-namespace --fleet <name> [--env-file ...]
    config = sub.add_parser("config", help="configuration helpers").add_subparsers(
        dest="config_command", required=True
    )
    migrate_env = config.add_parser(
        "migrate-env-namespace",
        help="add fleet-scoped variants of flat MAC_* credentials in ~/.mac/.env (mac-g55y)",
    )
    migrate_env.add_argument(
        "--fleet",
        required=True,
        help="fleet name to scope credentials under (e.g. rocky, jordanh-hub)",
    )
    migrate_env.add_argument(
        "--env-file",
        default=os.path.expanduser("~/.mac/.env"),
        help="path to the env file to migrate (default ~/.mac/.env)",
    )
    migrate_env.add_argument(
        "--drop-legacy",
        action="store_true",
        help="remove the flat unscoped keys after writing the scoped variants",
    )
    _set(cmd_config_migrate_env_namespace, migrate_env)

    login_parser = sub.add_parser(
        "login",
        help="bootstrap or inspect a scoped client login over verified SSH",
    )
    login_parser.add_argument(
        "login_action",
        nargs="?",
        choices=("status", "renew"),
        help="inspect or renew the selected login; omit to enroll",
    )
    login_parser.add_argument("--ssh", dest="ssh_target", help="hub SSH target user@host")
    login_parser.add_argument("--ssh-port", type=int)
    login_parser.add_argument("--proxy-jump")
    login_parser.add_argument("--identity-file")
    login_parser.add_argument("--known-hosts-file")
    login_parser.add_argument("--host-key-fingerprint")
    login_parser.add_argument("--host-ca")
    login_parser.add_argument("--fleet", dest="login_fleet")
    login_parser.add_argument("--agent", default=None)
    login_parser.add_argument("--fleets-config")
    login_parser.add_argument("--profile", dest="login_profile")
    login_parser.add_argument("--client-id")
    login_parser.add_argument("--name")
    login_parser.add_argument(
        "--scopes", default=",".join(("read", "write", "dispatch"))
    )
    login_parser.add_argument("--capabilities")
    login_parser.add_argument("--expires-in", type=int, default=30 * 24 * 60 * 60)
    login_parser.add_argument("--local-port", type=int)
    login_parser.add_argument("--remote-host", default="127.0.0.1")
    login_parser.add_argument("--remote-port", type=int)
    login_parser.add_argument("--allow-elevated", action="store_true")
    login_parser.add_argument(
        "--rotate",
        action="store_true",
        help="replace an existing client identity/profile explicitly",
    )
    login_parser.add_argument(
        "--remote-mac",
        default="mac",
        help="path to the mac executable on the hub; auto-discovered when omitted",
    )
    login_parser.add_argument("--connect-timeout", type=int, default=10)
    _set(cmd_login, login_parser)

    logout_parser = sub.add_parser(
        "logout", help="remove a local login and optionally revoke it on the hub"
    )
    logout_parser.add_argument("--profile", dest="logout_profile")
    logout_parser.add_argument(
        "--revoke",
        action="store_true",
        help="revoke the hub credential before deleting local secret state",
    )
    logout_parser.add_argument("--remote-mac", default="mac")
    logout_parser.add_argument("--connect-timeout", type=int, default=10)
    _set(cmd_logout, logout_parser)

    client = sub.add_parser(
        "client", help="hub enrollment principals and secure local client profiles"
    ).add_subparsers(dest="client_command", required=True)

    client_enroll = client.add_parser(
        "enroll",
        help="hub-local: mint a revocable scoped credential (invoke through SSH)",
    )
    client_enroll.add_argument("client_id")
    client_enroll.add_argument("--name")
    client_enroll.add_argument("--fleet", dest="fleet_name", default="")
    client_enroll.add_argument("--profile", dest="profile_name")
    client_enroll.add_argument("--scopes", default=",".join(("read", "write", "dispatch")))
    client_enroll.add_argument("--expires-in", type=int, default=30 * 24 * 60 * 60)
    client_enroll.add_argument("--api-url", default="http://127.0.0.1:8789")
    client_enroll.add_argument("--host-key-fingerprint")
    client_enroll.add_argument("--host-ca")
    client_enroll.add_argument("--capabilities")
    client_enroll.add_argument("--allow-elevated", action="store_true")
    client_enroll.add_argument(
        "--rotate", action="store_true", help="rotate an existing id instead of refusing"
    )
    client_enroll.add_argument("--registry", help="override the hub principal registry path")
    client_enroll.add_argument("--actor", default="ssh-operator")
    _set(cmd_client_enroll, client_enroll)

    client_renew = client.add_parser(
        "renew", help="hub-local: rotate one client's token and expiry"
    )
    client_renew.add_argument("client_id")
    client_renew.add_argument("--expires-in", type=int, default=30 * 24 * 60 * 60)
    client_renew.add_argument("--registry")
    client_renew.add_argument("--actor", default="ssh-operator")
    _set(cmd_client_renew, client_renew)

    client_revoke = client.add_parser(
        "revoke", help="hub-local: immediately revoke one client credential"
    )
    client_revoke.add_argument("client_id")
    client_revoke.add_argument("--registry")
    client_revoke.add_argument("--actor", default="ssh-operator")
    _set(cmd_client_revoke, client_revoke)

    client_list = client.add_parser(
        "list", help="hub-local: list client principals without token hashes"
    )
    client_list.add_argument("--registry")
    _set(cmd_client_list, client_list)

    client_profile = client.add_parser(
        "profile", help="install, select, inspect, or remove local secure profiles"
    ).add_subparsers(dest="client_profile_command", required=True)
    profile_install = client_profile.add_parser(
        "install", help="atomically install an enrollment manifest from JSON"
    )
    profile_install.add_argument("manifest", help="manifest file, or - for stdin")
    profile_install.add_argument("--profile", dest="profile_name")
    profile_install.add_argument("--no-activate", action="store_true")
    _set(cmd_client_profile_install, profile_install)
    profile_list = client_profile.add_parser("list")
    _set(cmd_client_profile_list, profile_list)
    profile_show = client_profile.add_parser("show")
    profile_show.add_argument("profile_name", nargs="?")
    _set(cmd_client_profile_show, profile_show)
    profile_activate = client_profile.add_parser("activate")
    profile_activate.add_argument("profile_name")
    _set(cmd_client_profile_activate, profile_activate)
    profile_remove = client_profile.add_parser("remove")
    profile_remove.add_argument("profile_name")
    _set(cmd_client_profile_remove, profile_remove)
    profile_migrate = client_profile.add_parser(
        "migrate-legacy",
        help="import one legacy fleet connection (admin token requires acknowledgement)",
    )
    profile_migrate.add_argument("--fleet", dest="fleet_name", required=True)
    profile_migrate.add_argument("--profile", dest="profile_name")
    profile_migrate.add_argument("--fleets-config")
    profile_migrate.add_argument("--env-file")
    profile_migrate.add_argument("--allow-legacy-admin-token", action="store_true")
    profile_migrate.add_argument("--no-activate", action="store_true")
    _set(cmd_client_profile_migrate_legacy, profile_migrate)

    tenant = sub.add_parser("tenant", help="tenant boundary commands").add_subparsers(dest="tenant_command", required=True)
    tenant_register = tenant.add_parser("register")
    tenant_register.add_argument("name")
    tenant_register.add_argument("--metadata")
    tenant_register.add_argument("--tenant-id")
    _set(cmd_tenant_register, tenant_register)
    tenant_list = tenant.add_parser("list")
    _set(cmd_tenant_list, tenant_list)

    user = sub.add_parser("user", help="human user identity commands").add_subparsers(dest="user_command", required=True)
    user_register = user.add_parser("register")
    user_register.add_argument("tenant_id")
    user_register.add_argument("handle")
    user_register.add_argument("--display-name")
    user_register.add_argument("--metadata")
    user_register.add_argument("--user-id")
    _set(cmd_user_register, user_register)

    persona = sub.add_parser("persona", help="Hermes persona and memory-scope commands").add_subparsers(dest="persona_command", required=True)
    persona_register = persona.add_parser("register")
    persona_register.add_argument("tenant_id")
    persona_register.add_argument("name")
    persona_register.add_argument("--soul-ref", required=True)
    persona_register.add_argument("--memory-scope", required=True)
    persona_register.add_argument("--metadata")
    persona_register.add_argument("--persona-id")
    _set(cmd_persona_register, persona_register)

    # Porting existed as a library for four weeks with no way to invoke it,
    # which is the same as not having it: the operator rule "port before you
    # switch" cannot be followed if there is no command that ports
    # (task_61b3f521).
    human_interface = sub.add_parser(
        "human-interface",
        help="port an agent profile between Hermes and OpenClaw",
    ).add_subparsers(dest="human_interface_command", required=True)
    hi_port = human_interface.add_parser(
        "port",
        help="port identity, memory and messaging credentials between interfaces",
    )
    hi_port.add_argument("--from", dest="source", required=True, choices=("hermes", "openclaw"))
    hi_port.add_argument("--to", dest="target", required=True, choices=("hermes", "openclaw"))
    hi_port.add_argument("--home", help="agent home (default: the current user's)")
    hi_port.add_argument("--state-file")
    # Dry-run by default: this rewrites an agent's identity and credentials,
    # so the caller has to say so.
    hi_port.add_argument(
        "--apply",
        action="store_true",
        help="actually write; without it this reports what would change",
    )
    _set(cmd_human_interface_port, hi_port)
    hi_check = human_interface.add_parser(
        "check",
        help="report whether switching to an interface would lose the profile",
    )
    hi_check.add_argument("--to", dest="target", required=True, choices=("hermes", "openclaw"))
    hi_check.add_argument("--home")
    hi_check.add_argument("--state-file")
    hi_check.add_argument("--max-age-seconds", type=float)
    hi_check.add_argument(
        "--assert-ready",
        action="store_true",
        help="exit non-zero when the target profile is not current",
    )
    _set(cmd_human_interface_check, hi_check)

    hermes = sub.add_parser("hermes", help="Hermes instance commands").add_subparsers(dest="hermes_command", required=True)
    hermes_register = hermes.add_parser("register")
    hermes_register.add_argument("tenant_id")
    hermes_register.add_argument("name")
    hermes_register.add_argument("--persona-id")
    hermes_register.add_argument("--home-ref")
    hermes_register.add_argument("--status", default="active")
    hermes_register.add_argument("--metadata")
    hermes_register.add_argument("--instance-id")
    _set(cmd_hermes_register, hermes_register)
    hermes_context = hermes.add_parser("context")
    hermes_context.add_argument("instance_id")
    _set(cmd_hermes_context, hermes_context)
    hermes_work_context = hermes.add_parser("work-context")
    hermes_work_context.add_argument("instance_id")
    hermes_work_context.add_argument("--active-only", action="store_true")
    hermes_work_context.add_argument("--task-limit", type=int, default=100)
    _set(cmd_hermes_work_context, hermes_work_context)
    hermes_runtime_proof = hermes.add_parser("runtime-proof")
    hermes_runtime_proof.add_argument("instance_id")
    hermes_runtime_proof.add_argument("--skip-startup-report", action="store_true")
    _set(cmd_hermes_runtime_proof, hermes_runtime_proof)

    binding = sub.add_parser("binding", help="Hermes platform binding commands").add_subparsers(dest="binding_command", required=True)
    binding_register = binding.add_parser("register")
    binding_register.add_argument("tenant_id")
    binding_register.add_argument("persona_instance_id")
    binding_register.add_argument("platform")
    binding_register.add_argument("external_id")
    binding_register.add_argument("--display-name")
    binding_register.add_argument("--scopes")
    binding_register.add_argument("--metadata")
    binding_register.add_argument("--binding-id")
    _set(cmd_binding_register, binding_register)

    interaction = sub.add_parser("interaction", help="create durable work from Hermes conversation context").add_subparsers(dest="interaction_command", required=True)
    interaction_task = interaction.add_parser("task")
    interaction_task.add_argument("persona_instance_id")
    interaction_task.add_argument("title")
    interaction_task.add_argument("--user-id")
    interaction_task.add_argument("--platform-binding-id")
    interaction_task.add_argument("--conversation-ref")
    interaction_task.add_argument("--description", default="")
    interaction_task.add_argument("--project")
    interaction_task.add_argument("--priority", type=int, default=0)
    interaction_task.add_argument("--required-capabilities")
    interaction_task.add_argument("--dependencies")
    interaction_task.add_argument("--metadata")
    interaction_task.add_argument("--max-attempts", type=int, default=3)
    interaction_task.add_argument("--actor", default="hermes")
    _set(cmd_interaction_task, interaction_task)

    task = sub.add_parser("task", help="task ledger commands").add_subparsers(dest="task_command", required=True)
    create = task.add_parser(
        "create", help="file a new task into the ledger"
    )
    create.add_argument("title")
    create.add_argument("--description", default="",
                        help="task description (use --description-file for multi-line / shell-hostile content)")
    create.add_argument("--description-file", dest="description_file",
                        help="read description from file path (or '-' for stdin); avoids shell-quoting hazards")
    create.add_argument(
        "--project",
        help="project to tag the task with; defaults to the working directory's "
        "project (git repo name, else cwd basename). Pass --project '' for none.",
    )
    create.add_argument("--priority", type=int, default=0)
    create.add_argument("--required-capabilities")
    create.add_argument("--dependencies")
    create.add_argument("--metadata",
                        help="JSON metadata (use --metadata-file for shell-hostile content)")
    create.add_argument("--metadata-file", dest="metadata_file",
                        help="read JSON metadata from file path (or '-' for stdin)")
    create.add_argument("--max-attempts", type=int, default=3)
    create.add_argument(
        "--kind",
        default="code",
        help="deliverable kind: 'code' (default; expects a repository change) or "
        "'report' (investigation/answer/triage — satisfied by a substantive "
        "operator_result, no diff required). Aliases for report: answer, "
        "analysis, investigation, question, triage.",
    )
    create.add_argument(
        "--model",
        default="",
        help="pin the LLM model BY NAME for THIS task only (e.g. a cheaper model "
        "for a simple task or a stronger one for complex work); agents pass it to "
        "their runtime/coding CLI and llm.route records it per completion",
    )
    create.add_argument(
        "--model-strength",
        type=int,
        default=None,
        metavar="1..10",
        help="pin the model by STRENGTH instead of name: 1 = cheapest/weakest .. "
        "10 = strongest/most expensive. Resolved to a concrete available model at "
        "run time, so it stays valid as model names change. --model wins if both.",
    )
    create.add_argument("--actor", default="human")
    create.add_argument(
        "--idempotency-key",
        default="",
        help="retry-safe create key (max 200 characters); the hub binds it "
             "durably to one request and task identity",
    )
    create.add_argument("--no-ticket", dest="no_ticket", action="store_true",
                        help="don't write the .tickets/<id>.md mirror for this task")
    create.add_argument("--no-dispatch", dest="no_dispatch", action="store_true",
                        help="BREAK-GLASS human hold: stage the task so the loop-mode fleet won't "
                             "auto-claim it (hidden from `task ready`) until started "
                             "explicitly. Not the normal path; auto-dispatch is.")
    create.add_argument(
        "--sync",
        dest="sync",
        action="store_true",
        help="run this task as a BARRIER on one worker: it starts only after "
             "that worker drains, nothing else runs while it does, and the "
             "worker accepts no new async work from the moment it is queued. "
             "Requires --target-agent. For work that mutates the worker "
             "itself, such as a sandbox image rollout.",
    )
    create.add_argument(
        "--target-agent",
        dest="target_agent",
        metavar="AGENT_ID",
        help="pin this task to one agent (required by --sync)",
    )
    wait = task.add_parser(
        "wait",
        help="wait for a project's tasks to finish, streaming each transition",
        description=(
            "Wait until every task in a project that is active -- or can become "
            "active -- has finished, printing each state change as it happens. "
            "Tasks that become blocked or need human input leave the wait and "
            "are reported, so the wait always terminates."
        ),
    )
    wait.add_argument("--project", default=None)
    wait.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="give up after this many seconds and exit 1 (default: wait forever)",
    )
    wait.add_argument(
        "--poll-interval",
        type=float,
        default=5.0,
        help="seconds between event polls (default: 5)",
    )
    wait.add_argument(
        "--rescan-interval",
        type=float,
        default=60.0,
        help="seconds between reconciliations against authoritative task state, "
             "which recover any events the feed missed (default: 60)",
    )
    wait.add_argument(
        "--no-stream",
        dest="no_stream",
        action="store_true",
        help="poll instead of following the hub's event stream (the fallback "
             "used automatically against a hub that predates /events/stream)",
    )
    wait.add_argument(
        "--no-follow-new",
        dest="no_follow_new",
        action="store_true",
        help="freeze the wait set at the tasks present when it started. By "
             "default a task created while waiting joins the set, so a task "
             "that decomposes into children does not let the wait return while "
             "its children are still running.",
    )
    _set(cmd_task_wait, wait)
    create.add_argument("--no-decompose", dest="no_decompose", action="store_true",
                        help="handoff/plan-note guard: the executor will not auto-decompose "
                             "this task into child tasks")
    create.add_argument(
        "--publication-lane",
        dest="publication_lane",
        choices=("auto", "managed", "legacy"),
        default="auto",
        help="publication route policy: auto uses the managed route for atomic "
             "repository tasks after fleet rollout, managed requires it, and "
             "legacy is the admin-only compatibility override",
    )
    _set(cmd_task_create, create)

    list_tasks = task.add_parser(
        "list",
        help="list tasks (default: short ids; use --full-ids for scripts)",
    )
    list_tasks.add_argument(
        "--selector",
        help="filter by attributes, e.g. 'state!=cancelled' or "
        "'state=blocked priority>=5'; `mac task help list` lists the keys",
    )
    list_tasks.add_argument("--state")
    list_tasks.add_argument("--project", help="filter to this project (default: the cwd's project)")
    list_tasks.add_argument("--all", action="store_true", help="every project (disable cwd scoping)")
    list_tasks.add_argument(
        "--limit",
        type=int,
        default=0,
        help="cap the number of returned tasks (default: no limit)",
    )
    list_tasks.add_argument(
        "--full-ids",
        dest="full_ids",
        action="store_true",
        default=False,
        help="show full 37-char task ids instead of the default short prefix (ignored by --json)",
    )
    _set(cmd_task_list, list_tasks)

    show = task.add_parser(
        "show",
        help="show task state, activity, diagnoses, and compact evidence counts "
        "(use --json for the complete structured record)",
    )
    show.add_argument("task_id")
    _set(cmd_task_show, show)

    summary = task.add_parser(
        "summary",
        help="activity-only task narrative (also included in `task show`)",
    )
    summary.add_argument("task_id")
    _set(cmd_task_summary, summary)

    ready = task.add_parser(
        "ready",
        help="list task-ready work and the number of currently eligible fleet agents",
    )
    ready.add_argument(
        "--limit",
        type=int,
        default=20,
        help="maximum ready tasks to explain (default: 20; server maximum: 100)",
    )
    ready.add_argument("--project", help="filter to this project (default: the cwd's project)")
    ready.add_argument("--all", action="store_true", help="every project (disable cwd scoping)")
    _set(cmd_task_ready, ready)

    why_unclaimed = task.add_parser(
        "why-unclaimed",
        help="show the authoritative task and agent reasons preventing a claim",
    )
    why_unclaimed.add_argument("task_id")
    _set(cmd_task_why_unclaimed, why_unclaimed)

    claim = task.add_parser("claim", help="atomically claim a task for an agent")
    claim.add_argument("task_id")
    claim.add_argument("agent_id")
    _set(cmd_task_claim, claim)

    break_glass = task.add_parser(
        "break-glass",
        help="admin-only: authorize an exact task/agent pair for single-use direct host execution",
    )
    break_glass.add_argument("task_id")
    break_glass.add_argument("agent_id")
    break_glass.add_argument("--reason", required=True)
    break_glass.add_argument(
        "--ttl-seconds",
        type=int,
        default=900,
        help="claim window before the authorization expires (60..3600; default 900)",
    )
    break_glass.add_argument("--actor", default="human")
    _set(cmd_task_break_glass_authorize, break_glass)

    break_glass_list = task.add_parser(
        "break-glass-list",
        help="list durable break-glass authorizations for a task",
    )
    break_glass_list.add_argument("task_id")
    break_glass_list.add_argument("--limit", type=int, default=100)
    _set(cmd_task_break_glass_list, break_glass_list)

    break_glass_revoke = task.add_parser(
        "break-glass-revoke",
        help="admin-only: revoke an unclaimed host authorization",
    )
    break_glass_revoke.add_argument("authorization_id")
    break_glass_revoke.add_argument("--reason", required=True)
    break_glass_revoke.add_argument("--actor", default="human")
    _set(cmd_task_break_glass_revoke, break_glass_revoke)

    close = task.add_parser(
        "close",
        help="transition a task to completed/cancelled; cancellation requires a reason",
    )
    close.add_argument("task_id")
    close.add_argument(
        "--reason",
        default="",
        help="audit reason (required with --cancelled)",
    )
    close.add_argument("--actor", default="human")
    close.add_argument("--no-ticket", dest="no_ticket", action="store_true",
                       help="don't update the .tickets/<id>.md mirror on close")
    close.add_argument("--cancelled", dest="success", action="store_false",
                       help="close as CANCELLED instead of COMPLETED")
    close.add_argument(
        "--disposition",
        choices=CANCELLATION_DISPOSITIONS,
        help="why cancelled work should be preserved or eventually cleaned up "
        "(default: preserve)",
    )
    close.add_argument(
        "--replacement-task",
        help="replacement task required for duplicate or superseded cancellations",
    )
    close.add_argument(
        "--cleanup-grace-days",
        type=float,
        default=7.0,
        help="delay before an eligible managed ref may be pruned (default: 7)",
    )
    close.set_defaults(success=True)
    _set(cmd_task_close, close)

    cancel = task.add_parser(
        "cancel",
        help="actively cancel a task, revoke its lease, and abort a running worker executor",
    )
    cancel.add_argument("task_id", nargs="?")
    cancel.add_argument(
        "--reason",
        default="operator requested cancellation",
        help="audit reason (default: operator requested cancellation)",
    )
    cancel.add_argument("--actor", default="human")
    cancel.add_argument(
        "--no-ticket",
        dest="no_ticket",
        action="store_true",
        help="don't update the .tickets/<id>.md compatibility mirror",
    )
    cancel.add_argument(
        "--disposition",
        choices=CANCELLATION_DISPOSITIONS,
        help="why cancelled work should be preserved or eventually cleaned up "
        "(default: preserve)",
    )
    cancel.add_argument(
        "--replacement-task",
        help="replacement task required for duplicate or superseded cancellations",
    )
    cancel.add_argument(
        "--cleanup-grace-days",
        type=float,
        default=7.0,
        help="delay before an eligible managed ref may be pruned (default: 7)",
    )
    cancel.add_argument(
        "--selector",
        help="cancel every task an expression names, e.g. "
        "'state=failed project=nanolang'. DRY BY DEFAULT: shows what it "
        "would touch and changes nothing until --apply",
    )
    cancel.add_argument(
        "--apply",
        action="store_true",
        help="actually perform a --selector cancellation",
    )

    _set(cmd_task_cancel, cancel)

    ask = task.add_parser(
        "ask",
        help="park a task on an unanswered human question (not a failure; never reaped)",
    )
    ask.add_argument("task_id")
    ask.add_argument(
        "--question",
        action="append",
        required=True,
        help="a question that must be answered before work can continue (repeatable)",
    )
    ask.add_argument("--why", default="", help="why the answer is needed")
    ask.add_argument("--actor", default="human")
    _set(cmd_task_ask, ask)

    needs_input = task.add_parser(
        "needs-input",
        help="list tasks parked on an unanswered human question (the operator inbox)",
    )
    needs_input.add_argument(
        "--all", action="store_true", help="every project, not just the cwd's"
    )
    needs_input.add_argument("--limit", type=int, default=None)
    _set(cmd_task_needs_input, needs_input)

    edit = task.add_parser(
        "edit",
        help="answer a parked task in $EDITOR; saving submits it back to the queue",
    )
    edit.add_argument("task_id")
    edit.add_argument("--actor", default="human")
    _set(cmd_task_edit, edit)

    answer = task.add_parser(
        "answer",
        help="record the answer to a parked question and dispose of the task "
        "(resumes by default; --cancel when the answer is 'not needed')",
    )
    answer.add_argument("task_id")
    answer.add_argument("--answer", required=True, help="the answer to record")
    answer.add_argument("--actor", default="human")
    answer.add_argument(
        "--disposition",
        default="resume",
        choices=("resume", *CANCELLATION_DISPOSITIONS),
        help="what the answer means for the task: 'resume' releases it to the "
        "dispatch pool; any cancellation disposition closes it with that "
        "reason (superseded, duplicate, not_applicable, deferred, ...)",
    )
    answer.add_argument(
        "--replaced-by",
        dest="replaced_by",
        default=None,
        metavar="TASK_ID",
        help="the task that supersedes this one; requires a closing "
        "--disposition and records the pointer, failing if the id does not "
        "resolve",
    )
    _set(cmd_task_answer, answer)

    reopen = task.add_parser(
        "reopen",
        help="recovery: return a stuck/terminal task (failed/cancelled/blocked) to OPEN for retry or reconciliation",
    )
    reopen.add_argument("task_id")
    reopen.add_argument("--reason", default="")
    reopen.add_argument("--actor", default="human")
    _set(cmd_task_reopen, reopen)

    recover_stranded = task.add_parser(
        "recover-stranded",
        help="re-supervise tasks left waiting on a terminal dependency "
        "(dry-run unless --apply)",
    )
    recover_stranded.add_argument(
        "--limit",
        type=int,
        default=500,
        help="maximum terminal dependencies to reconcile in one pass",
    )
    recover_stranded.add_argument(
        "--apply",
        action="store_true",
        help="actually re-supervise; without it the command only reports",
    )
    _set(cmd_task_recover_stranded, recover_stranded)

    recover_finalizer = task.add_parser(
        "recover-finalizer",
        help="revalidate and publish preserved work refused for uncommitted new files",
    )
    recover_finalizer.add_argument(
        "workspace",
        help="preserved agent task workspace containing task.json and repository-worktree.json",
    )
    recover_finalizer.add_argument(
        "--approve-new-file",
        action="append",
        default=[],
        help="exact intended new path; repeat for every refused new file",
    )
    recover_finalizer.add_argument(
        "--evidence-id",
        help="original executor evidence id recorded in recovery provenance",
    )
    recover_finalizer.add_argument(
        "--execute",
        action="store_true",
        help="commit, revalidate, and push; omit for a read-only recovery plan",
    )
    _set(cmd_task_recover_finalizer, recover_finalizer)

    recover_stalled = task.add_parser(
        "recover-stalled-finalizer",
        help="resume a deterministic finalizer that stalled (timeout/cancel/crash) after harvesting verified work",
    )
    recover_stalled.add_argument(
        "workspace",
        help="preserved agent task workspace containing task.json, repository-worktree.json, and finalizer-progress.json",
    )
    recover_stalled.add_argument(
        "--approve-new-file",
        action="append",
        default=[],
        help="exact intended new path; repeat for every uncommitted new file",
    )
    recover_stalled.add_argument(
        "--evidence-id",
        help="original executor evidence id recorded in recovery provenance",
    )
    recover_stalled.add_argument(
        "--execute",
        action="store_true",
        help="commit, revalidate, and push; omit for a read-only recovery plan",
    )
    _set(cmd_task_recover_stalled_finalizer, recover_stalled)

    force_complete = task.add_parser(
        "force-complete",
        help="BREAK-GLASS operator override: mark a task COMPLETED regardless of state/review (bypasses the adversarial auto-land gate; audited). Not the normal path — the adversarial reviewer + contract gate auto-land is.",
    )
    force_complete.add_argument("task_id")
    force_complete.add_argument("--reason", default="")
    force_complete.add_argument("--actor", default="human")
    _set(cmd_task_force_complete, force_complete)

    select = task.add_parser(
        "select",
        help="preview the group of tasks a selector expression names",
        description=(
            "Selector terms: key=value, key!=value, key~contains, key>=n, "
            "key<=n; comma-separated values mean any-of. Keys: id, state, "
            "project, title, description, text, owner, priority, attempts, "
            "capability, metadata.<path>, unmet, group."
        ),
    )
    select.add_argument("selector", help="e.g. 'state=needs_input project=mac'")
    select.add_argument("--limit", type=int, default=None)
    select.add_argument("--sample", type=int, default=20, help="tasks to show")
    _set(cmd_task_select, select)

    batch = task.add_parser(
        "batch",
        help="apply one operation to every task a selector names (dry by default)",
    )
    batch.add_argument("operation", choices=list(BATCH_OPERATIONS))
    batch.add_argument("selector")
    batch.add_argument(
        "--apply",
        action="store_true",
        help="actually write; without this the command only reports what would change",
    )
    batch.add_argument("--actor", default="human")
    batch.add_argument("--limit", type=int, default=None)
    batch.add_argument(
        "--expect-count",
        type=int,
        default=None,
        help="refuse if the group is no longer this size",
    )
    batch.add_argument(
        "--expect-token",
        default=None,
        help="refuse if the group is no longer the one previewed (from select)",
    )
    batch.add_argument("--answer", help="answer operation: the answer text")
    batch.add_argument("--reason")
    batch.add_argument("--disposition")
    batch.add_argument("--replacement-task")
    batch.add_argument("--target-state", help="close operation: target state")
    batch.add_argument("--title")
    batch.add_argument("--description")
    batch.add_argument("--project")
    batch.add_argument("--priority", type=int)
    batch.add_argument("--max-attempts", type=int)
    batch.add_argument("--required-capabilities", help="comma-separated")
    batch.add_argument(
        "--metadata-merge",
        help="JSON object deep-merged into each task's metadata",
    )
    batch.add_argument(
        "--metadata-replace",
        help=(
            "JSON object REPLACING each task's metadata wholesale. The dry run "
            "reports every key the group would lose, load-bearing ones first."
        ),
    )
    batch.add_argument(
        "--metadata-set",
        help='JSON object of dotted paths to assign, e.g. \'{"triage.owner":"jordan"}\'',
    )
    batch.add_argument(
        "--metadata-unset",
        help="comma-separated dotted paths to remove, e.g. triage.owner,stale",
    )
    _set(cmd_task_batch, batch)

    group = task.add_parser("group", help="named, saved task groups").add_subparsers(
        dest="task_group_command", required=True
    )
    group_save = group.add_parser("save", help="create or update a named group")
    group_save.add_argument("name")
    group_save.add_argument("selector")
    group_save.add_argument("--description")
    group_save.add_argument("--actor", default="human")
    _set(cmd_task_group_save, group_save)
    group_list = group.add_parser("list", help="list saved groups")
    _set(cmd_task_group_list, group_list)
    group_show = group.add_parser("show", help="show one saved group")
    group_show.add_argument("name")
    _set(cmd_task_group_show, group_show)
    group_delete = group.add_parser("delete", help="delete a saved group")
    group_delete.add_argument("name")
    _set(cmd_task_group_delete, group_delete)

    search = task.add_parser("search", help="keyword search across task title and description")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=50)
    search.add_argument("--project", help="filter to this project (default: the cwd's project)")
    search.add_argument("--all", action="store_true", help="every project (disable cwd scoping)")
    _set(cmd_task_search, search)

    stats = task.add_parser("stats", help="count tasks by state")
    stats.add_argument("--project", help="filter to this project (default: the cwd's project)")
    stats.add_argument("--all", action="store_true", help="every project (disable cwd scoping)")
    _set(cmd_task_stats, stats)

    _set(
        cmd_task_generator_yield,
        task.add_parser(
            "generator-yield",
            help="show each task origin's completion yield and whether the "
            "yield gate is letting it file",
        ),
    )

    throughput = task.add_parser(
        "throughput",
        help="task-to-main KPIs, stage dwell, stranded work, and resource collisions",
    )
    throughput.add_argument(
        "--project",
        help="filter to this project (default: the cwd's project)",
    )
    throughput.add_argument(
        "--all",
        action="store_true",
        help="every project (disable cwd scoping)",
    )
    throughput.add_argument(
        "--since-hours",
        type=float,
        default=24.0,
        help="completion/contention window in hours (default: 24)",
    )
    throughput.add_argument(
        "--warning-minutes",
        type=float,
        default=5.0,
        help="stage dwell that opens a stranding episode (default: 5)",
    )
    throughput.add_argument(
        "--critical-minutes",
        type=float,
        default=10.0,
        help="stage dwell that marks an episode critical (default: 10)",
    )
    throughput.add_argument(
        "--refresh-limit",
        type=int,
        default=100,
        help="maximum stale task histories to repair in this query (0-500; default: 100)",
    )
    _set(cmd_task_throughput, throughput)

    audit = task.add_parser(
        "audit",
        help="read-only reconciliation of every task's history, evidence, dependencies, replacements, and git ancestry",
    )
    audit.add_argument(
        "--project",
        help="audit only this project (default: every project; cwd scoping is intentionally disabled)",
    )
    audit.add_argument(
        "--no-git",
        action="store_true",
        help="skip repository ancestry checks (results remain unverified)",
    )
    audit.add_argument("--offset", type=int, default=0, help="zero-based task page offset")
    audit.add_argument("--limit", type=int, help="maximum tasks to audit (1-500)")
    _set(cmd_task_audit, audit)

    start = task.add_parser(
        "start", help="move a claimed task to RUNNING as its lease holder"
    )
    start.add_argument("task_id")
    start.add_argument("agent_id")
    start.add_argument("--lease-id")
    _set(cmd_task_start, start)

    release = task.add_parser(
        "release", help="clear a --no-dispatch hold so the task can auto-dispatch"
    )
    release.add_argument("task_id")
    release.add_argument("--actor", default="human")
    _set(cmd_task_release, release)

    submit = task.add_parser(
        "submit-review",
        help="hand a running task to the adversarial reviewer (to NEEDS_REVIEW)",
    )
    submit.add_argument("task_id")
    submit.add_argument("agent_id")
    submit.add_argument("--lease-id")
    _set(cmd_task_submit, submit)

    evidence = task.add_parser(
        "evidence",
        help="attach evidence to a task: the record a review and auto-land read",
    )
    evidence.add_argument("task_id")
    evidence.add_argument(
        "--kind",
        required=True,
        help="evidence kind (one of: %s)" % ", ".join(EVIDENCE_KIND_CHOICES),
    )
    evidence.add_argument("--uri", required=True)
    evidence.add_argument("--summary", required=True)
    evidence.add_argument("--created-by", required=True)
    evidence.add_argument("--checksum")
    evidence.add_argument("--lease-id")
    evidence.add_argument("--metadata")
    _set(cmd_task_evidence, evidence)

    detect_beads = task.add_parser(
        "detect-beads",
        help="inspect a repo for .beads/ artifacts (read-only)",
    )
    detect_beads.add_argument("repo_path")
    _set(cmd_task_detect_beads, detect_beads)

    migrate_beads = task.add_parser(
        "migrate-beads",
        help="import .beads/issues.jsonl into MAC tasks and emit .tickets/<id>.md",
    )
    migrate_beads.add_argument("repo_path")
    migrate_beads.add_argument("--project", required=True)
    migrate_beads.add_argument("--actor", default="beads-migrator")
    migrate_beads.add_argument("--dry-run", action="store_true")
    migrate_beads.add_argument("--no-tickets", action="store_true",
                               help="skip writing .tickets/<id>.md files")
    migrate_beads.add_argument("--no-memories", action="store_true",
                               help="skip importing bd memories")
    migrate_beads.add_argument("--tickets-only", action="store_true",
                               help="write .tickets/<id>.md mirror only; skip MAC db writes "
                                    "(useful for repos not registered with a MAC hub)")
    _set(cmd_task_migrate_beads, migrate_beads)

    # Connector-aware (preferred): works for any future ticketing system, not
    # just beads. detect-ticketing reports the needs-conversion signal hermes
    # uses to ask the user; convert-ticketing runs the one-way import.
    detect_ticketing = task.add_parser(
        "detect-ticketing",
        help="detect ticketing sources in a repo (.tickets local mirror / .beads foreign) "
        "and whether a one-way conversion should be offered (read-only)",
    )
    detect_ticketing.add_argument("repo_path")
    _set(cmd_task_detect_ticketing, detect_ticketing)

    convert_ticketing = task.add_parser(
        "convert-ticketing",
        help="one-way convert a detected foreign source (e.g. beads) into MAC "
        "ledger tasks plus optional local compatibility files (run after the user agrees)",
    )
    convert_ticketing.add_argument("repo_path")
    convert_ticketing.add_argument("--project", required=True)
    convert_ticketing.add_argument("--actor", default="hermes")
    convert_ticketing.add_argument("--dry-run", action="store_true")
    _set(cmd_task_convert_ticketing, convert_ticketing)

    repo = sub.add_parser(
        "repo", help="managed repository work-ref lifecycle commands"
    ).add_subparsers(dest="repo_command", required=True)
    repo_refs = repo.add_parser(
        "refs", help="audit or prune task-owned remote branches"
    ).add_subparsers(dest="repo_refs_command", required=True)
    refs_audit = repo_refs.add_parser(
        "audit", help="classify managed refs without changing the repository"
    )
    refs_prune = repo_refs.add_parser(
        "prune", help="show or execute safe exact-SHA managed-ref cleanup"
    )
    refs_status = repo_refs.add_parser(
        "status", help="show the hub's automatic repository-ref reconciler status"
    )
    refs_reconcile = repo_refs.add_parser(
        "reconcile", help="ask the hub to reconcile all registered repositories now"
    )
    for refs_parser in (refs_audit, refs_prune):
        refs_parser.add_argument(
            "--repo",
            dest="repo_path",
            default=".",
            help="repository working tree (default: current directory)",
        )
        refs_parser.add_argument("--remote", default="origin")
        refs_parser.add_argument(
            "--base-ref",
            help="local canonical ref used to prove completed work is merged "
            "(default: <remote>/main)",
        )
        refs_parser.add_argument(
            "--task",
            dest="task_ids",
            action="append",
            help="limit to one task ID (repeatable)",
        )
        refs_parser.add_argument(
            "--grace-days",
            type=float,
            default=7.0,
            help="fallback grace period for legacy completed refs (default: 7)",
        )
    _set(cmd_repo_refs_audit, refs_audit)
    prune_mode = refs_prune.add_mutually_exclusive_group()
    prune_mode.add_argument(
        "--execute",
        action="store_true",
        help="delete eligible unchanged refs; default is a read-only dry-run",
    )
    prune_mode.add_argument(
        "--dry-run",
        dest="execute",
        action="store_false",
        help="explicitly request the default read-only mode",
    )
    refs_prune.set_defaults(execute=False)
    refs_prune.add_argument("--actor", default="human")
    _set(cmd_repo_refs_prune, refs_prune)
    _set(cmd_repo_refs_status, refs_status)
    refs_reconcile.add_argument(
        "--mode",
        choices=("audit", "prune"),
        help="override the configured mode for this run",
    )
    refs_reconcile.add_argument("--actor", default="human")
    _set(cmd_repo_refs_reconcile, refs_reconcile)

    project = sub.add_parser("project", help="project summary commands").add_subparsers(dest="project_command", required=True)
    project_egress = project.add_parser(
        "egress",
        help="hosts every task in this project may reach from its sandbox",
        description=(
            "Declared at PROJECT level because a repository has one egress "
            "contract and many tasks. The hub projects it onto each assignment "
            "at claim time, so it applies to tasks created before the "
            "declaration too, and it strips any task-level value -- these hosts "
            "are operator policy, not a task author's request."
        ),
    ).add_subparsers(dest="project_egress_command", required=True)

    project_egress_list = project_egress.add_parser("list", help="show declared hosts")
    project_egress_list.add_argument("project")
    _set(cmd_project_egress_list, project_egress_list)

    project_egress_grant = project_egress.add_parser("grant", help="allow one host")
    project_egress_grant.add_argument("project")
    project_egress_grant.add_argument("host", help="a plain DNS hostname")
    project_egress_grant.add_argument("--reason", help="why the project needs it")
    _set(cmd_project_egress_grant, project_egress_grant)

    project_egress_revoke = project_egress.add_parser("revoke", help="withdraw one host")
    project_egress_revoke.add_argument("project")
    project_egress_revoke.add_argument("host")
    _set(cmd_project_egress_revoke, project_egress_revoke)

    project_create = project.add_parser(
        "create", help="create a project and its dispatch policy"
    )
    project_create.add_argument("name")
    project_create.add_argument("--description", default="")
    project_create.add_argument("--metadata", default="{}")
    project_create.add_argument("--status", default="active")
    project_create.add_argument("--actor", default="human")
    project_create.add_argument("--project-id")
    project_dispatch = project_create.add_mutually_exclusive_group()
    project_dispatch.add_argument(
        "--paused",
        dest="dispatch_paused",
        action="store_true",
        default=True,
        help="stage the project: its tickets will not auto-dispatch until activated (default)",
    )
    project_dispatch.add_argument(
        "--active",
        dest="dispatch_paused",
        action="store_false",
        help="open the project to autonomous dispatch immediately",
    )
    _set(cmd_project_create, project_create)
    project_register = project.add_parser(
        "register",
        help="register the current checkout, a local path, or GIT_URL[#BRANCH] "
        "as a project and create its contract-authoring task",
    )
    project_register.add_argument(
        "repository_url",
        nargs="?",
        metavar="PATH|GIT_URL[#BRANCH]",
        help="checkout path or git remote plus optional working branch; "
        "defaults to the current checkout",
    )
    project_register.add_argument(
        "--project",
        help="project name (default: repo name for main, repo@branch otherwise)",
    )
    project_register.add_argument(
        "--branch",
        "--default-branch",
        dest="default_branch",
        help="working branch override; local checkouts otherwise use origin/HEAD "
        "then the current branch, while URLs default to main",
    )
    project_register.add_argument(
        "--title", help="override the contract-authoring task title"
    )
    project_register.add_argument("--priority", type=int, default=0)
    project_register.add_argument(
        "--required-capabilities",
        help="comma-separated capabilities required to claim the onboarding task",
    )
    project_register.add_argument("--actor", default="human")
    _set(cmd_project_register, project_register)
    project_pause = project.add_parser(
        "pause", help="hold a project's tickets from autonomous dispatch"
    )
    project_pause.add_argument("project")
    project_pause.add_argument("--actor", default="human")
    _set(cmd_project_pause, project_pause)
    project_activate = project.add_parser(
        "activate", help="open a project to autonomous dispatch"
    )
    project_activate.add_argument("project")
    project_activate.add_argument("--actor", default="human")
    _set(cmd_project_activate, project_activate)
    sandbox = sub.add_parser(
        "sandbox",
        help="derive, check, and roll out the OpenShell sandbox image",
        description="derive, check, and roll out the OpenShell sandbox image",
    ).add_subparsers(dest="sandbox_command", required=True)
    sandbox_bom_cmd = sandbox.add_parser(
        "bom",
        help="derive the sandbox bill of materials from every project's contract",
    )
    sandbox_bom_cmd.add_argument(
        "--write",
        metavar="PATH",
        help="write the reviewed manifest (commit it; the image hash covers it)",
    )
    sandbox_bom_cmd.add_argument(
        "--compare",
        metavar="PATH",
        help="compare live contracts against a committed manifest; exit 1 on drift",
    )
    sandbox_bom_cmd.add_argument(
        "--containerfile",
        metavar="PATH",
        help="also report what the derived BOM requires that this image never mentions",
    )
    _set(cmd_sandbox_bom, sandbox_bom_cmd)
    sandbox_rollout_cmd = sandbox.add_parser(
        "rollout",
        help="roll a reviewed sandbox image onto each worker, after it drains",
    )
    sandbox_rollout_cmd.add_argument(
        "--image",
        required=True,
        help="the immutable GHCR digest to install (not a tag)",
    )
    sandbox_rollout_cmd.add_argument(
        "--manifest",
        help="the reviewed BOM manifest to record on each rollout task",
    )
    sandbox_rollout_cmd.add_argument("--project", default=None)
    sandbox_rollout_cmd.add_argument("--actor", default="human")
    _set(cmd_sandbox_rollout, sandbox_rollout_cmd)
    project_list = project.add_parser("list", help="list every project")
    _set(cmd_project_list, project_list)
    project_show = project.add_parser(
        "show", help="show one project: policy, repositories, dispatch state"
    )
    project_show.add_argument("project")
    _set(cmd_project_show, project_show)
    project_update = project.add_parser(
        "update",
        help="update project fields or its branch-qualified repository registration",
    )
    project_update.add_argument("project")
    project_update.add_argument("--name")
    project_update.add_argument("--description")
    project_update.add_argument("--metadata")
    project_update.add_argument(
        "--status",
        choices=("active", "inactive", "archived"),
    )
    project_update.add_argument(
        "--registration",
        dest="repository_registration",
        metavar="GIT_URL[#BRANCH]",
        help="replace the canonical repository registration",
    )
    project_update.add_argument(
        "--branch",
        "--default-branch",
        dest="default_branch",
        help="change the working branch while keeping the current Git URL",
    )
    project_update.add_argument("--actor", default="human")
    _set(cmd_project_update, project_update)
    project_unregister = project.add_parser(
        "unregister",
        help="remove a project; --force detaches historical tasks and disables "
        "linked checkout registrations",
    )
    project_unregister.add_argument("project")
    project_unregister.add_argument("--force", action="store_true")
    project_unregister.add_argument("--actor", default="human")
    _set(cmd_project_unregister, project_unregister)

    work_package = sub.add_parser(
        "work-package",
        help="versioned work-DAG admission, readiness, and controller stages",
    ).add_subparsers(dest="work_package_command", required=True)
    wp_admit = work_package.add_parser(
        "admit",
        help="compile, attest, and atomically materialize a held plan",
    )
    wp_plan = wp_admit.add_mutually_exclusive_group(required=True)
    wp_plan.add_argument("--plan", help="inline JSON plan")
    wp_plan.add_argument(
        "--plan-file",
        help="JSON plan path, or '-' for stdin (recommended)",
    )
    wp_admit.add_argument("--reason", required=True)
    wp_admit.add_argument("--actor", default="work-package-admission-controller")
    wp_admit.add_argument("--tenant-id")
    wp_admit.add_argument("--root-task-id")
    _set(cmd_work_package_admit, wp_admit)
    wp_list = work_package.add_parser("list", help="list work packages")
    wp_list.add_argument("--state")
    wp_list.add_argument(
        "--selector",
        help="filter by attributes, e.g. 'state!=cancelled' or "
        "'state=blocked priority>=5'; `mac work-package help list` lists the keys",
    )
    wp_list.add_argument("--project")
    wp_list.add_argument("--limit", type=int, default=100)
    _set(cmd_work_package_list, wp_list)
    wp_show = work_package.add_parser(
        "show", help="show one work package: member tasks, plan, certification state"
    )
    wp_show.add_argument("package_id")
    _set(cmd_work_package_show, wp_show)
    wp_readiness = work_package.add_parser(
        "readiness",
        help="show credential/capability activation blockers",
    )
    wp_readiness.add_argument("package_id")
    _set(cmd_work_package_readiness, wp_readiness)
    wp_update = work_package.add_parser(
        "update", help="change a work package's goal or metadata (not its plan)"
    )
    wp_update.add_argument("package_id")
    wp_update.add_argument("--goal")
    wp_update.add_argument("--metadata")
    wp_update.add_argument("--actor", default="human")
    _set(cmd_work_package_update, wp_update)
    wp_cancel = work_package.add_parser(
        "cancel",
        aliases=["delete"],
        help="terminally abandon a work package (nothing hard-deletes one)",
    )
    wp_cancel.add_argument("package_id")
    wp_cancel.add_argument("--reason", required=True)
    wp_cancel.add_argument("--actor", default="human")
    _set(cmd_work_package_cancel, wp_cancel)
    wp_activate = work_package.add_parser(
        "activate", help="open an admitted work package for execution"
    )
    wp_activate.add_argument("package_id")
    wp_activate.add_argument("--plan-version", type=int, required=True)
    wp_activate.add_argument("--epoch", type=int, required=True)
    wp_activate.add_argument("--actor", default="human")
    _set(cmd_work_package_activate, wp_activate)
    wp_replan_preview = work_package.add_parser(
        "replan-preview",
        help="compile and attest plan N+1, then report whether it can be applied",
    )
    wp_replan_preview.add_argument("package_id")
    wp_replan_preview_plan = wp_replan_preview.add_mutually_exclusive_group(
        required=True
    )
    wp_replan_preview_plan.add_argument("--plan", help="inline JSON replacement plan")
    wp_replan_preview_plan.add_argument(
        "--plan-file",
        help="replacement plan path, or '-' for stdin (recommended)",
    )
    wp_replan_preview.add_argument("--plan-version", type=int, required=True)
    wp_replan_preview.add_argument("--epoch", type=int, required=True)
    wp_replan_preview.add_argument("--reason", required=True)
    wp_replan_preview.add_argument(
        "--actor",
        default="work-package-replan-controller",
    )
    _set(cmd_work_package_replan_preview, wp_replan_preview)
    wp_pause = work_package.add_parser(
        "pause",
        help="raise the package Andon by exact plan-version and epoch CAS",
    )
    wp_pause.add_argument("package_id")
    wp_pause.add_argument("--plan-version", type=int, required=True)
    wp_pause.add_argument("--epoch", type=int, required=True)
    wp_pause.add_argument("--reason", required=True)
    wp_pause.add_argument("--actor", default="human")
    _set(cmd_work_package_pause, wp_pause)
    wp_replan = work_package.add_parser(
        "replan",
        help="atomically install one paused package's compiled replacement plan",
    )
    wp_replan.add_argument("package_id")
    wp_replan_plan = wp_replan.add_mutually_exclusive_group(required=True)
    wp_replan_plan.add_argument("--plan", help="inline JSON replacement plan")
    wp_replan_plan.add_argument(
        "--plan-file",
        help="replacement plan path, or '-' for stdin (recommended)",
    )
    wp_replan.add_argument("--plan-version", type=int, required=True)
    wp_replan.add_argument("--epoch", type=int, required=True)
    wp_replan.add_argument("--reason", required=True)
    wp_replan.add_argument(
        "--actor",
        default="work-package-replan-controller",
    )
    _set(cmd_work_package_replan, wp_replan)
    wp_verify = work_package.add_parser(
        "verify-output",
        help="controller-observe an immutable attempt and append its receipt",
    )
    wp_verify.add_argument("evidence_id")
    wp_verify.add_argument("--actor", default="work-package-output-controller")
    _set(cmd_work_package_verify_output, wp_verify)
    wp_accept_candidate = work_package.add_parser(
        "accept-candidate",
        help="accept one reviewed, controller-verified package candidate",
    )
    wp_accept_candidate.add_argument("candidate_id")
    wp_accept_candidate.add_argument(
        "--actor",
        default="work-package-acceptance-controller",
    )
    _set(cmd_work_package_accept_candidate, wp_accept_candidate)
    wp_reject_candidate = work_package.add_parser(
        "reject-candidate",
        help="reject one package candidate and stage its bounded rework decision",
    )
    wp_reject_candidate.add_argument("candidate_id")
    wp_reject_candidate.add_argument("--reason", required=True)
    wp_reject_candidate.add_argument(
        "--actor",
        default="work-package-acceptance-controller",
    )
    _set(cmd_work_package_reject_candidate, wp_reject_candidate)
    wp_assemble = work_package.add_parser(
        "assemble",
        help="freeze and assemble exact accepted inputs for one integration node",
    )
    wp_assemble.add_argument("package_id")
    wp_assemble.add_argument("integration_node_key")
    wp_assemble.add_argument(
        "--actor",
        default="work-package-integration-controller",
    )
    _set(cmd_work_package_assemble, wp_assemble)
    wp_assembly_status = work_package.add_parser(
        "assembly-status",
        help="show an integrity-checked integration-batch snapshot",
    )
    wp_assembly_status.add_argument("batch_id")
    _set(cmd_work_package_assembly_status, wp_assembly_status)
    wp_assembly_claim = work_package.add_parser(
        "assembly-claim",
        help="claim an integration batch under the controller fence",
    )
    wp_assembly_claim.add_argument("batch_id")
    _set(cmd_work_package_assembly_claim, wp_assembly_claim)
    wp_assemble_batch = work_package.add_parser(
        "assemble-batch",
        help="assemble a previously-created integration batch",
    )
    wp_assemble_batch.add_argument("batch_id")
    _set(cmd_work_package_assemble_batch, wp_assemble_batch)
    wp_certification_prepare = work_package.add_parser(
        "certification-prepare",
        help="prepare an immutable external-certifier job for an exact batch",
    )
    wp_certification_prepare.add_argument("batch_id")
    wp_certification_prepare.add_argument("bundle_path")
    wp_certification_prepare.add_argument(
        "--actor",
        default="work-package-certification-controller",
    )
    _set(cmd_work_package_certification_prepare, wp_certification_prepare)
    wp_certification_status = work_package.add_parser(
        "certification-status",
        help="show an integrity-checked certification job snapshot",
    )
    wp_certification_status.add_argument("job_id")
    _set(cmd_work_package_certification_status, wp_certification_status)
    wp_certification_claim = work_package.add_parser(
        "certification-claim",
        help="claim a certification job under its monotonic controller fence",
    )
    wp_certification_claim.add_argument("job_id")
    wp_certification_claim.add_argument("--owner")
    _set(cmd_work_package_certification_claim, wp_certification_claim)
    wp_certification_ingest = work_package.add_parser(
        "certification-ingest",
        help="ingest one exact fenced external-certifier result",
    )
    wp_certification_ingest.add_argument("job_id")
    wp_certification_result = wp_certification_ingest.add_mutually_exclusive_group(
        required=True
    )
    wp_certification_result.add_argument("--result", help="inline result JSON")
    wp_certification_result.add_argument(
        "--result-file",
        help="result JSON path, or '-' for stdin (recommended)",
    )
    wp_certification_ingest.add_argument("--owner", required=True)
    wp_certification_ingest.add_argument("--fence", required=True, type=int)
    _set(cmd_work_package_certification_ingest, wp_certification_ingest)
    wp_certification_run = work_package.add_parser(
        "certification-run",
        help="explicitly run and ingest a prepared OpenShell certification job",
    )
    wp_certification_run.add_argument("job_id")
    wp_certification_run.add_argument("bundle_path")
    wp_certification_run.add_argument("--owner")
    wp_certification_run.add_argument("--result-path")
    _set(cmd_work_package_certification_run, wp_certification_run)
    wp_reject_certification = work_package.add_parser(
        "reject-failed-certification",
        help="read back the atomic Andon disposition for a failed certification",
    )
    wp_reject_certification.add_argument("batch_id")
    wp_reject_certification.add_argument("certification_id")
    wp_reject_certification.add_argument(
        "--actor",
        default="work-package-certification-controller",
    )
    _set(cmd_work_package_reject_failed_certification, wp_reject_certification)
    wp_accept_certification = work_package.add_parser(
        "accept-certification",
        help="accept one exact passed certification for landing",
    )
    wp_accept_certification.add_argument("batch_id")
    wp_accept_certification.add_argument("certification_id")
    _set(cmd_work_package_accept_certification, wp_accept_certification)
    wp_land = work_package.add_parser(
        "land",
        help="land one accepted exact candidate on its registered repository",
    )
    wp_land.add_argument("batch_id")
    _set(cmd_work_package_land, wp_land)
    wp_finalize_publication = work_package.add_parser(
        "finalize-publication",
        help="consume the exact landing receipt and complete the product graph",
    )
    wp_finalize_publication.add_argument("batch_id")
    wp_finalize_publication.add_argument(
        "--actor",
        default="work-package-publication-finalizer",
    )
    wp_finalize_publication.add_argument("--receipt-id")
    _set(cmd_work_package_finalize_publication, wp_finalize_publication)

    directive = sub.add_parser(
        "directive",
        help="versioned fleet rules, conditional bindings, and held workflow macros",
    ).add_subparsers(
        dest="directive_command", required=True
    )
    directive_propose = directive.add_parser(
        "propose", help="validate and create an immutable directive version"
    )
    directive_propose.add_argument(
        "--document-file",
        required=True,
        help="directive YAML/JSON path, or '-' for stdin",
    )
    directive_propose.add_argument("--actor", default="human")
    _set(cmd_directive_propose, directive_propose)

    directive_list = directive.add_parser("list")
    directive_list.add_argument("--state")
    _set(cmd_directive_list, directive_list)

    directive_show = directive.add_parser("show")
    directive_show.add_argument("directive")
    _set(cmd_directive_show, directive_show)

    directive_versions = directive.add_parser("versions")
    directive_versions.add_argument("directive")
    _set(cmd_directive_versions, directive_versions)

    directive_check = directive.add_parser(
        "check", help="analyze exact policy, bindings, conflicts, and macro effects"
    )
    directive_check.add_argument("directive")
    directive_check.add_argument("--version", type=int)
    directive_check.add_argument("--actor", default="human")
    _set(cmd_directive_check, directive_check)

    directive_impact = directive.add_parser("impact")
    directive_impact.add_argument("directive")
    _set(cmd_directive_impact, directive_impact)

    directive_approve = directive.add_parser(
        "approve", help="approve one exact passing check and immutable digest"
    )
    directive_approve.add_argument("directive")
    directive_approve.add_argument("--version", required=True, type=int)
    directive_approve.add_argument("--digest", required=True)
    directive_approve.add_argument("--check-id", required=True)
    directive_approve.add_argument("--actor", default="human")
    _set(cmd_directive_approve, directive_approve)

    directive_activate = directive.add_parser(
        "activate", help="distribute an approved version for fleet acknowledgement"
    )
    directive_activate.add_argument("directive")
    directive_activate.add_argument("--version", required=True, type=int)
    directive_activate.add_argument("--digest", required=True)
    directive_activate.add_argument("--actor", default="human")
    _set(cmd_directive_activate, directive_activate)

    directive_deactivate = directive.add_parser("deactivate")
    directive_deactivate.add_argument("directive")
    directive_deactivate.add_argument("--reason")
    directive_deactivate.add_argument("--reason-file")
    directive_deactivate.add_argument("--actor", default="human")
    _set(cmd_directive_deactivate, directive_deactivate)

    directive_effective = directive.add_parser(
        "effective", help="render the currently active policy snapshot"
    )
    directive_effective.add_argument("--repository-id")
    directive_effective.add_argument("--project")
    _set(cmd_directive_effective, directive_effective)

    directive_binding = directive.add_parser(
        "binding", help="hub-owned variable bindings"
    ).add_subparsers(dest="directive_binding_command", required=True)
    directive_binding_set = directive_binding.add_parser("set")
    directive_binding_set.add_argument("target_type", choices=("fleet", "project", "repository"))
    directive_binding_set.add_argument("target_id")
    directive_binding_set.add_argument("key")
    directive_binding_set.add_argument("--value", help="inline JSON value")
    directive_binding_set.add_argument(
        "--value-file", help="JSON value path, or '-' for stdin"
    )
    directive_binding_set.add_argument("--actor", default="human")
    _set(cmd_directive_binding_set, directive_binding_set)
    directive_binding_list = directive_binding.add_parser("list")
    directive_binding_list.add_argument("--target-type", choices=("fleet", "project", "repository"))
    directive_binding_list.add_argument("--target-id")
    directive_binding_list.add_argument("--inactive", action="store_true")
    _set(cmd_directive_binding_list, directive_binding_list)

    directive_waiver = directive.add_parser(
        "waiver", help="audited exact-version repository/project exceptions"
    ).add_subparsers(dest="directive_waiver_command", required=True)
    directive_waiver_create = directive_waiver.add_parser("create")
    directive_waiver_create.add_argument("directive")
    directive_waiver_create.add_argument("--version", required=True, type=int)
    directive_waiver_create.add_argument("--target-type", required=True, choices=("project", "repository"))
    directive_waiver_create.add_argument("--target-id", required=True)
    directive_waiver_create.add_argument("--reason")
    directive_waiver_create.add_argument("--reason-file")
    directive_waiver_create.add_argument("--expires-at")
    directive_waiver_create.add_argument("--actor", default="human")
    _set(cmd_directive_waiver_create, directive_waiver_create)
    directive_waiver_list = directive_waiver.add_parser("list")
    directive_waiver_list.add_argument("--directive")
    _set(cmd_directive_waiver_list, directive_waiver_list)
    directive_waiver_revoke = directive_waiver.add_parser("revoke")
    directive_waiver_revoke.add_argument("waiver")
    directive_waiver_revoke.add_argument("--reason")
    directive_waiver_revoke.add_argument("--reason-file")
    directive_waiver_revoke.add_argument("--actor", default="human")
    _set(cmd_directive_waiver_revoke, directive_waiver_revoke)

    hgx = sub.add_parser(
        "hgx",
        help="operator controls for fungible HGX provider capacity",
    ).add_subparsers(dest="hgx_command", required=True)
    hgx_capacity = hgx.add_parser(
        "capacity",
        help="plan, inspect, or explicitly create bounded standard-dind capacity",
    ).add_subparsers(dest="hgx_capacity_command", required=True)
    hgx_capacity_status = hgx_capacity.add_parser(
        "status",
        help="read provider inventory and durable attestation receipts",
    )
    _add_hgx_capacity_args(hgx_capacity_status)
    _set(cmd_hgx_capacity_status, hgx_capacity_status)
    hgx_capacity_plan = hgx_capacity.add_parser(
        "plan",
        help="show the bounded capacity action without applying it",
    )
    _add_hgx_capacity_args(hgx_capacity_plan)
    _set(cmd_hgx_capacity_plan, hgx_capacity_plan)
    hgx_capacity_execute = hgx_capacity.add_parser(
        "execute",
        help=(
            "create standard-dind sessions within bounds and require nonce SSH "
            "attestation; never deletes sessions"
        ),
    )
    _add_hgx_capacity_args(hgx_capacity_execute)
    _set(cmd_hgx_capacity_execute, hgx_capacity_execute)
    hgx_capacity_mark_onboarded = hgx_capacity.add_parser(
        "mark-onboarded",
        help=(
            "consume an attested capacity receipt after the session is "
            "registered as a real MAC agent"
        ),
    )
    hgx_capacity_mark_onboarded.add_argument("session_id")
    hgx_capacity_mark_onboarded.add_argument("--agent-id", required=True)
    from mac.hgx_elastic_capacity import DEFAULT_STATE_PATH

    hgx_capacity_mark_onboarded.add_argument(
        "--state-file",
        default=DEFAULT_STATE_PATH,
        help="durable controller receipt path",
    )
    _set(cmd_hgx_capacity_mark_onboarded, hgx_capacity_mark_onboarded)

    openshell = sub.add_parser("openshell", help="OpenShell sandbox guardrail commands").add_subparsers(dest="openshell_command", required=True)
    osh_reconcile = openshell.add_parser(
        "reconcile",
        help="reconcile fleet OpenShell required/policy/deployment status after host validation",
    )
    osh_reconcile.add_argument(
        "--apply",
        action="store_true",
        help="mutate hub state; default is a dry-run diff",
    )
    osh_reconcile.add_argument(
        "--validated",
        action="store_true",
        help="assert that host runtime validation has passed; required to apply status=active",
    )
    osh_reconcile.add_argument(
        "--agent",
        action="append",
        help="agent id or name to reconcile; repeatable. Defaults to enabled Linux agents in fleets.yaml",
    )
    osh_reconcile.add_argument(
        "--target-fleet",
        help="fleet in fleets.yaml to read when --agent is omitted; defaults to --fleet or the config default",
    )
    osh_reconcile.add_argument(
        "--fleet-config",
        help="path to fleets.yaml (default: $MAC_FLEETS_CONFIG or ~/.mac/fleets.yaml)",
    )
    osh_reconcile.add_argument(
        "--strict",
        action="store_true",
        help="fail if any fleets.yaml-selected agent is missing from the hub registry",
    )
    osh_reconcile.add_argument("--policy-name", default="mac-docker-engine-moby")
    osh_reconcile.add_argument(
        "--policy-file",
        help="policy YAML to create/reuse/update; default deploy/openshell/mac-hermes-policy.yaml",
    )
    osh_reconcile.add_argument(
        "--status",
        default="active",
        choices=("active", "starting", "inactive", "degraded", "failed", "unknown"),
    )
    osh_reconcile.add_argument("--actor", default="human")
    osh_reconcile.add_argument("--runtime", default="docker-engine-moby")
    osh_reconcile.add_argument("--openshell-version", default="0.0.72")
    osh_reconcile.add_argument("--gateway-driver", default="docker")
    osh_reconcile.add_argument("--image", default="localhost/mac-hermes:net")
    osh_reconcile.add_argument("--sandbox-id")
    osh_reconcile.add_argument("--validation-summary")
    osh_reconcile.add_argument("--detail", help="JSON status detail object")
    osh_reconcile.add_argument("--detail-file", help="read JSON status detail from file path or '-'")
    osh_reconcile.add_argument(
        "--no-report-status",
        action="store_true",
        help="only reconcile required resources and policy assignment",
    )
    _set(cmd_openshell_reconcile, osh_reconcile)

    osh_gc = openshell.add_parser(
        "sandbox-gc",
        help="list or delete old orphaned MAC-owned OpenShell sandboxes",
    )
    osh_gc.add_argument(
        "--apply",
        action="store_true",
        help="delete eligible sandboxes; default is a dry-run",
    )
    osh_gc.add_argument(
        "--stale-after-hours",
        type=float,
        default=24.0,
        help="minimum sandbox age before deletion (default: 24)",
    )
    osh_gc.add_argument(
        "--no-legacy",
        action="store_true",
        help="only consider labeled sandboxes, not legacy MAC name prefixes",
    )
    osh_gc.add_argument(
        "--openshell-bin",
        default=os.environ.get("MAC_OPENSHELL_BIN") or "openshell",
    )
    _set(cmd_openshell_sandbox_gc, osh_gc)

    osh_reap = openshell.add_parser(
        "reap-orphans",
        help="fail-closed reap of MAC-owned task sandboxes with mac.keep=false "
        "and a dead recorded PID (no age wait)",
    )
    osh_reap.add_argument(
        "--apply",
        action="store_true",
        help="delete reap-eligible orphaned sandboxes; default is a dry-run",
    )
    osh_reap.add_argument(
        "--openshell-bin",
        default=os.environ.get("MAC_OPENSHELL_BIN") or "openshell",
    )
    _set(cmd_openshell_reap_orphans, osh_reap)

    osh_render = openshell.add_parser(
        "render-policy",
        help="render the OpenShell guardrail policy from the operator template for this fleet",
    )
    osh_render.add_argument("--agent-user", required=True, help="home owner on the agent host (e.g. jkh)")
    osh_render.add_argument("--hub-host", required=True, help="MAC hub host (e.g. 100.125.137.89)")
    osh_render.add_argument("--hub-port", type=int, default=8789)
    osh_render.add_argument("--model-gateway-host", help="LLM gateway host (default: hub host)")
    osh_render.add_argument(
        "--image-runtime",
        help="in-image runtime path (e.g. /opt/mac-venv) when the sandbox runs a "
        "prebuilt --from image instead of a host-uploaded runtime; caches -> /tmp",
    )
    osh_render.add_argument("--qdrant-port", type=int, default=6333)
    osh_render.add_argument("--firecrawl-port", type=int, default=3002)
    osh_render.add_argument(
        "--template",
        default=str(Path(__file__).resolve().parents[2] / "deploy" / "openshell" / "mac-hermes-policy.yaml"),
        help="operator policy template path",
    )
    osh_render.add_argument("--into", help="write the rendered policy here (default: stdout)")
    _set(cmd_openshell_render_policy, osh_render)

    osh_policy = openshell.add_parser("policy", help="MAC-managed OpenShell policies").add_subparsers(
        dest="openshell_policy_command", required=True
    )
    osh_policy_create = osh_policy.add_parser("create")
    osh_policy_create.add_argument("name")
    osh_policy_create.add_argument("--policy-text")
    osh_policy_create.add_argument("--policy-file", help="read policy YAML from file path or '-'")
    osh_policy_create.add_argument("--description", default="")
    osh_policy_create.add_argument("--metadata")
    osh_policy_create.add_argument("--metadata-file")
    osh_policy_create.add_argument("--created-by", default="human")
    osh_policy_create.add_argument("--policy-id")
    _set(cmd_openshell_policy_create, osh_policy_create)

    osh_policy_list = osh_policy.add_parser("list")
    osh_policy_list.add_argument("--include-deleted", action="store_true")
    _set(cmd_openshell_policy_list, osh_policy_list)

    osh_policy_show = osh_policy.add_parser("show")
    osh_policy_show.add_argument("policy")
    _set(cmd_openshell_policy_show, osh_policy_show)

    osh_policy_update = osh_policy.add_parser("update")
    osh_policy_update.add_argument("policy")
    osh_policy_update.add_argument("--name")
    osh_policy_update.add_argument("--description")
    osh_policy_update.add_argument("--policy-text")
    osh_policy_update.add_argument("--policy-file")
    osh_policy_update.add_argument("--metadata")
    osh_policy_update.add_argument("--metadata-file")
    osh_policy_update.add_argument("--updated-by", default="human")
    _set(cmd_openshell_policy_update, osh_policy_update)

    osh_policy_delete = osh_policy.add_parser("delete")
    osh_policy_delete.add_argument("policy")
    osh_policy_delete.add_argument("--actor", default="human")
    _set(cmd_openshell_policy_delete, osh_policy_delete)

    osh_policy_render = osh_policy.add_parser("render")
    osh_policy_render.add_argument("policy")
    osh_policy_render.add_argument("--agent-user")
    osh_policy_render.add_argument("--hub-host")
    osh_policy_render.add_argument("--hub-port", type=int)
    osh_policy_render.add_argument("--model-gateway-host")
    osh_policy_render.add_argument("--shared-services")
    osh_policy_render.add_argument("--shared-services-file")
    osh_policy_render.add_argument("--into")
    _set(cmd_openshell_policy_render, osh_policy_render)

    osh_policy_assign = osh_policy.add_parser("assign")
    osh_policy_assign.add_argument("policy")
    osh_policy_assign.add_argument("target_id")
    osh_policy_assign.add_argument("--target-type", default="agent", choices=("agent", "fleet", "host"))
    osh_policy_assign.add_argument("--created-by", default="human")
    _set(cmd_openshell_policy_assign, osh_policy_assign)

    osh_policy_versions = osh_policy.add_parser("versions")
    osh_policy_versions.add_argument("policy")
    _set(cmd_openshell_policy_versions, osh_policy_versions)

    osh_policy_deploy_status = osh_policy.add_parser("deploy-status")
    osh_policy_deploy_status.add_argument("--agent", required=True)
    _set(cmd_openshell_policy_deploy_status, osh_policy_deploy_status)

    osh_status = openshell.add_parser("status")
    osh_status.add_argument("--agent", required=True)
    _set(cmd_openshell_status, osh_status)

    machine = sub.add_parser("machine", help="machine registry commands").add_subparsers(dest="machine_command", required=True)
    machine_register = machine.add_parser("register")
    machine_register.add_argument("hostname")
    machine_register.add_argument("--labels")
    machine_register.add_argument("--resources")
    machine_register.add_argument("--untrusted", action="store_true")
    machine_register.add_argument("--machine-id")
    _set(cmd_machine_register, machine_register)

    machine_list = machine.add_parser("list", help="list all registered machines")
    _set(cmd_machine_list, machine_list)

    machine_show = machine.add_parser("show", help="show full record for one machine")
    machine_show.add_argument("machine_id")
    _set(cmd_machine_show, machine_show)

    agent = sub.add_parser("agent", help="agent registry commands").add_subparsers(dest="agent_command", required=True)
    agent_register = agent.add_parser(
        "register", help="register a new agent onto a machine"
    )
    agent_register.add_argument("machine_id")
    agent_register.add_argument("name")
    agent_register.add_argument("--capabilities")
    agent_register.add_argument("--resources")
    agent_register.add_argument("--agent-id")
    agent_register.add_argument(
        "--persona-instance-id",
        dest="persona_instance_id",
        help="Persona instance id to link this agent to.",
    )
    agent_register.add_argument(
        "--instance-kind", choices=("static", "fungible"), default=None
    )
    _set(cmd_agent_register, agent_register)

    agent_update = agent.add_parser(
        "update", help="change an agent's capabilities, status or metadata"
    )
    agent_update.add_argument("agent_id")
    agent_update.add_argument(
        "--instance-kind", choices=("static", "fungible"), required=False
    )
    agent_update.add_argument(
        "--capabilities",
        help="comma-separated capability list, replacing the current set",
    )
    agent_update.add_argument(
        "--add-capability", action="append",
        help="add one capability, keeping the rest (repeatable)",
    )
    agent_update.add_argument(
        "--remove-capability", action="append",
        help="remove one capability, keeping the rest (repeatable)",
    )
    _set(cmd_agent_update, agent_update)


    egress = task.add_parser(
        "egress",
        help="declare which hosts a task's sandbox may reach",
        description=(
            "Hosts declared here are granted at the hub_declared trust tier, "
            "because top-level task metadata is set through an authenticated "
            "hub credential. Hosts the worker derives from repo content are "
            "untrusted and refused unless they match the reviewed registry."
        ),
    ).add_subparsers(dest="egress_command", required=True)

    egress_list = egress.add_parser("list", help="show a task's declared hosts")
    egress_list.add_argument("task_id")
    _set(cmd_task_egress_list, egress_list)

    egress_grant = egress.add_parser("grant", help="allow one host")
    egress_grant.add_argument("task_id")
    egress_grant.add_argument("host", help="a plain DNS hostname, e.g. api.example.com")
    egress_grant.add_argument("--reason", help="why this task needs it")
    egress_grant.add_argument("--actor", default="human")
    _set(cmd_task_egress_grant, egress_grant)

    egress_revoke = egress.add_parser("revoke", help="withdraw one host")
    egress_revoke.add_argument("task_id")
    egress_revoke.add_argument("host")
    egress_revoke.add_argument("--actor", default="human")
    _set(cmd_task_egress_revoke, egress_revoke)
    task_update = task.add_parser(
        "update",
        help="change a task's fields (title, description, project, priority, "
        "capabilities, metadata, max attempts)",
    )
    task_update.add_argument("task_id")
    task_update.add_argument("--title")
    task_update.add_argument(
        "--description",
        help="new description (use --description-file for multi-line / shell-hostile content)",
    )
    task_update.add_argument("--description-file", dest="description_file")
    task_update.add_argument("--project")
    task_update.add_argument("--priority", type=int)
    task_update.add_argument("--max-attempts", dest="max_attempts", type=int)
    task_update.add_argument(
        "--capabilities", help="comma-separated required capabilities (replaces the set)"
    )
    task_update.add_argument(
        "--metadata", help="JSON metadata (use --metadata-file for shell-hostile content)"
    )
    task_update.add_argument("--metadata-file", dest="metadata_file")
    task_update.add_argument("--actor", default="human")
    _set(cmd_task_update, task_update)

    agent_show = agent.add_parser("show", help="show one agent")
    agent_show.add_argument("agent_id")
    _set(cmd_agent_show, agent_show)

    agent_list = agent.add_parser(
        "list", help="list agents (--health adds liveness)"
    )
    agent_list.add_argument("--health", action="store_true")
    agent_list.add_argument(
        "--selector",
        help="filter by attributes, e.g. 'state!=cancelled' or "
        "'state=blocked priority>=5'; `mac agent help list` lists the keys",
    )
    _set(cmd_agent_list, agent_list)

    agent_attestation_recover = agent.add_parser(
        "attestation-recover",
        help="admin-only conditional recovery for a missing/stale worker signing key",
    )
    agent_attestation_recover.add_argument("agent_id")
    agent_attestation_recover.add_argument("--probe-file", required=True)
    agent_attestation_recover.add_argument("--manifest-out", required=True)
    _set(cmd_agent_attestation_recover, agent_attestation_recover)

    report_executor_approve = agent.add_parser(
        "report-executor-approve",
        help="approve the exact current startup-attested OpenShell report executor",
    )
    report_executor_approve.add_argument("agent_id")
    report_executor_approve.add_argument("--attestation-file", required=True)
    report_executor_approve.add_argument("--startup-timestamp", required=True)
    report_executor_approve.add_argument("--actor", default="fleet-deploy")
    _set(cmd_agent_report_executor_approve, report_executor_approve)

    report_executor_revoke = agent.add_parser(
        "report-executor-revoke",
        help="revoke report-repository dispatch eligibility for an agent",
    )
    report_executor_revoke.add_argument("agent_id")
    report_executor_revoke.add_argument("--reason", required=True)
    report_executor_revoke.add_argument("--actor", default="fleet-deploy")
    _set(cmd_agent_report_executor_revoke, report_executor_revoke)

    agent_reflect = agent.add_parser(
        "reflect",
        help="publish an agent's runtime self-description over AgentBus",
    )
    agent_reflect.add_argument("agent_id")
    agent_reflect.add_argument("--recipient-agent-id")
    agent_reflect.add_argument("--request-id")
    agent_reflect.add_argument(
        "--reflect-timeout",
        type=float,
        default=30.0,
        help=(
            "seconds to poll for the target agent's live reflect narrative "
            "(default 30; 0 skips the blocking wait and returns the published "
            "inventory immediately)"
        ),
    )
    _set(cmd_agent_reflect, agent_reflect)

    agent_hardware = agent.add_parser(
        "hardware", help="fleet hardware inventory from self-reported resources.hardware"
    )
    _set(cmd_agent_hardware, agent_hardware)

    heartbeat = agent.add_parser(
        "heartbeat",
        help="report an agent alive, with its status, health, and resources",
    )
    heartbeat.add_argument("agent_id")
    heartbeat.add_argument("--status")
    heartbeat.add_argument("--health-status")
    heartbeat.add_argument("--resources")
    heartbeat.add_argument(
        "--running-digest",
        help="runtime_environments.digest declaring which build this agent is running",
    )
    _set(cmd_agent_heartbeat, heartbeat)

    agent_tell = agent.add_parser(
        "tell",
        help="send a hub-verified HUMAN directive to any agent over AgentBus — "
        "works for Slack-less agents (GKE runners, ephemeral sessions); the "
        "receiver can trust its operator provenance by construction",
    )
    agent_tell.add_argument("agent", help="agent id or name")
    agent_tell.add_argument("message")
    agent_tell.add_argument("--issued-by", help="who is speaking (default: human)")
    agent_tell.add_argument(
        "--task",
        help="bind this directive to a task the agent is actively executing; "
        "the hub verifies the agent owns a current lease and routes the "
        "directive to the active task executor (not a persona chat turn)",
    )
    agent_tell.add_argument(
        "--wait", type=float, help="wait up to N seconds for the agent's reply"
    )
    _set(cmd_agent_tell, agent_tell)

    agent_deregister = agent.add_parser(
        "deregister",
        help="graceful exit for a session/ephemeral agent: optionally leave one "
        "final human-facing message (delivered after the agent is gone), then "
        "tombstone with history preserved",
    )
    agent_deregister.add_argument("agent_id")
    agent_deregister.add_argument("--actor")
    agent_deregister.add_argument(
        "--final-message", help="one last status message to deliver on exit"
    )
    agent_deregister.add_argument(
        "--final-target", help="delivery target for --final-message (e.g. channel:C123)"
    )
    _set(cmd_agent_deregister, agent_deregister)

    agent_delete = agent.add_parser(
        "delete",
        help="decommission (tombstone) an agent: strips operational overlays "
        "(moods/naps/config flags/deploy config) but preserves its AgentBus "
        "streams, events, deliveries, and task history",
    )
    agent_delete.add_argument("agent_id")
    agent_delete.add_argument("--actor", default="human")
    _set(cmd_agent_delete, agent_delete)

    agent_hold = agent.add_parser(
        "hold",
        help="place a dispatch hold on an agent; held agents are skipped during claim-next",
    )
    agent_hold.add_argument("agent_id")
    agent_hold.add_argument("--reason", required=True, help="human-readable reason for the hold")
    _set(cmd_agent_hold, agent_hold)

    agent_resume = agent.add_parser(
        "resume",
        help="remove the dispatch hold from an agent, making it eligible for dispatch again",
    )
    agent_resume.add_argument("agent_id")
    _set(cmd_agent_resume, agent_resume)

    agent_config = agent.add_parser(
        "config",
        help="consolidated per-agent configuration (the 'geek knobs')",
    ).add_subparsers(dest="agent_config_command", required=True)
    agent_config_show = agent_config.add_parser(
        "show",
        help="every effective knob for one agent from one place: identity, "
        "runtime flags, gateway-reported deploy config, mood",
    )
    agent_config_show.add_argument("agent", help="agent id or name")
    _set(cmd_agent_config_show, agent_config_show)

    agent_migrate = agent.add_parser(
        "migrate",
        help="move an agent (soul + memory) to a new host; dry-run unless --execute",
    )
    agent_migrate.add_argument("name")
    agent_migrate.add_argument("--to", dest="to_target", required=True, help="destination user@host")
    agent_migrate.add_argument("--from", dest="from_target", help="source user@host (default: current fleets.yaml target)")
    agent_migrate.add_argument("--to-os", default="linux")
    agent_migrate.add_argument("--fleet", help="fleet name (default: auto-resolve from fleets.yaml)")
    agent_migrate.add_argument("--execute", action="store_true", help="run it (default: print the plan)")
    agent_migrate.add_argument("--keep-source", action="store_true", help="don't decommission the source host")
    agent_migrate.add_argument("--retire-source-agent", help="agent_id to `mac agent delete` after migration")
    hub_grp = agent_migrate.add_mutually_exclusive_group()
    hub_grp.add_argument(
        "--hub", dest="hub", action="store_true", default=None,
        help="full-fidelity HUB migration: also move mac.db + Qdrant + MAC_SECRET_KEY "
             "(auto-detected when the agent is the fleet's hub_agent/shared_services_manager)")
    hub_grp.add_argument(
        "--no-hub", dest="hub", action="store_false",
        help="force soul-only (spoke) migration even if the agent looks like the hub")
    agent_migrate.add_argument("--src-os", help="source service manager (default: from fleets.yaml, else linux)")
    agent_migrate.add_argument("--to-ssh-port", type=int)
    agent_migrate.add_argument("--to-identity-file")
    agent_migrate.add_argument("--to-proxy-jump")
    agent_migrate.add_argument("--to-known-hosts-file")
    agent_migrate.add_argument(
        "--to-host-key-policy", choices=("strict", "accept-new", "insecure")
    )
    _set(cmd_agent_migrate, agent_migrate)

    fleet = sub.add_parser("fleet", help="fleet-wide queries").add_subparsers(
        dest="fleet_command", required=True
    )
    fleet_build = fleet.add_parser(
        "build-distribution",
        help="aggregate live agents by running_digest",
    )
    _set(cmd_fleet_build_distribution, fleet_build)

    # mac-fleet-target: authoritative per-role version pin (target of record).
    # Backed by the checked-in deploy/openclaw/fleet-target.json manifest
    # (schema mac.fleet_target.v1), not the hub DB, so deploy hosts, canaries,
    # and CI can read the pin without a populated control plane.
    fleet_target = fleet.add_parser(
        "target",
        help="authoritative per-role fleet version pin (source rev + OpenClaw VERSION/REVISION)",
    ).add_subparsers(dest="fleet_target_command")
    fleet_target.required = True

    ft_set = fleet_target.add_parser("set", help="pin the target of record for a role")
    ft_set.add_argument("role", help="fleet role name (e.g. gateway, worker)")
    ft_set.add_argument("source", help="MAC source revision (git commit) to pin")
    ft_set.add_argument(
        "--openclaw-version", help="OpenClaw gateway VERSION (roles that run the gateway)"
    )
    ft_set.add_argument(
        "--openclaw-revision", help="OpenClaw image REVISION (numeric build id or commit hash)"
    )
    ft_set.add_argument("--manifest", help="override manifest path (defaults to the checked-in file)")
    _set(cmd_fleet_target_set, ft_set)

    ft_get = fleet_target.add_parser("get", help="show the pinned target for one role")
    ft_get.add_argument("role", help="fleet role name")
    ft_get.add_argument("--manifest", help="override manifest path")
    _set(cmd_fleet_target_get, ft_get)

    ft_show = fleet_target.add_parser("show", help="show the full target-of-record manifest")
    ft_show.add_argument("--manifest", help="override manifest path")
    _set(cmd_fleet_target_show, ft_show)

    ft_list = fleet_target.add_parser("list", help="list every pinned role target")
    ft_list.add_argument("--manifest", help="override manifest path")
    _set(cmd_fleet_target_list, ft_list)

    # mac-backlog-groom: autonomous per-repo backlog grooming — status, manual
    # run, and per-project opt-in.
    fleet_groom = fleet.add_parser(
        "backlog-groom",
        help="autonomous backlog grooming: status, manual run, per-project opt-in",
    )
    groom_sub = fleet_groom.add_subparsers(dest="backlog_groom_command")
    groom_sub.required = True
    _set(cmd_fleet_backlog_groom_status, groom_sub.add_parser(
        "status", help="show groomer config + last run report (hub read)"))
    _set(cmd_fleet_backlog_groom_run, groom_sub.add_parser(
        "run", help="trigger one immediate grooming pass across opted-in idle repos"))
    groom_enable = groom_sub.add_parser("enable", help="opt a project into backlog grooming")
    groom_enable.add_argument("project", help="project name (must be onboarded)")
    groom_enable.add_argument("--backlog-size", type=int, default=None,
                              help="number of backlog items to request per grooming pass")
    groom_enable.add_argument("--min-ready", type=int, default=None,
                              help="only groom when the project has fewer than N pending tasks")
    groom_enable.add_argument("--capability", action="append", default=None,
                              help="required capability to stamp on the grooming task; repeatable")
    _set(cmd_fleet_backlog_groom_enable, groom_enable)
    groom_disable = groom_sub.add_parser("disable", help="opt a project out of backlog grooming")
    groom_disable.add_argument("project", help="project name")
    _set(cmd_fleet_backlog_groom_disable, groom_disable)

    # mac-model-select: dynamic powerhouse-model selection. A swap is recorded
    # pending and only changes routing when promoted (operator/eval gate).
    fleet_msel = fleet.add_parser(
        "model-selection",
        help="dynamic powerhouse-model selection: status, refresh, promote a pending swap",
    )
    msel_sub = fleet_msel.add_subparsers(dest="model_selection_command")
    msel_sub.required = True
    _set(cmd_fleet_model_selection_status, msel_sub.add_parser(
        "status", help="show active + pending selection and last refresh"))
    _set(cmd_fleet_model_selection_refresh, msel_sub.add_parser(
        "refresh", help="refresh now (a swap is recorded pending, not adopted)"))
    _set(cmd_fleet_model_selection_promote, msel_sub.add_parser(
        "promote", help="promote the pending swap to active (routing changes here)"))

    fleet_ssh_spec = fleet.add_parser(
        "ssh-spec",
        help="resolve the canonical secret-free SSH route for one fleet agent",
    )
    fleet_ssh_spec.add_argument(
        "--fleet", dest="fleet_name", help="fleet key/name (defaults like global --fleet)"
    )
    fleet_ssh_spec.add_argument("--agent", help="agent name (default: fleet hub_agent)")
    fleet_ssh_spec.add_argument(
        "--fleets-config", default=str(mac_paths.fleets_config())
    )
    fleet_ssh_spec.add_argument("--ssh-port", type=int)
    fleet_ssh_spec.add_argument(
        "--portable",
        action="store_true",
        help="require explicit identity and host-key material suitable for a clean HOME",
    )
    _set(cmd_fleet_ssh_spec, fleet_ssh_spec)

    fleet_refresh = fleet.add_parser(
        "refresh-source",
        aliases=["refresh"],
        help=(
            "ask fleet agents to pull their self-update repo and restart "
            "themselves if HEAD changes"
        ),
    )
    fleet_refresh.add_argument(
        "--sender-agent-id",
        help="registered admin/control agent id to send the message as; defaults to MAC_AGENT_ID",
    )
    fleet_refresh.add_argument(
        "--agent-id",
        action="append",
        help="target one agent id; repeatable. Default targets every agent.",
    )
    fleet_refresh.add_argument("--repo-path")
    fleet_refresh.add_argument("--remote", default="origin")
    fleet_refresh.add_argument("--branch", default="main")
    fleet_refresh.add_argument("--request-id")
    fleet_refresh.add_argument("--no-restart", action="store_true")
    fleet_refresh.add_argument(
        "--restart-service",
        action="append",
        help=(
            "systemd service to restart on hosts where it is installed after a "
            "successful source update; repeatable"
        ),
    )
    _set(cmd_fleet_refresh_source, fleet_refresh)

    # fleet-02: live group awareness for the team.
    fleet_snap = fleet.add_parser(
        "snapshot",
        help="compact view of the fleet: who's online + what each agent is working on",
    )
    fleet_snap.add_argument("--agent", help="exclude this agent id (the caller) from the snapshot")
    _set(cmd_fleet_snapshot, fleet_snap)

    # Phase 1 fleet snapshot: pull/edit/push the editable agent soul text layer.
    fleet_soul_pull = fleet.add_parser(
        "soul-pull",
        help="pull each agent's editable soul text (SOUL/USER/MEMORY.md) into a local tree",
    )
    fleet_soul_pull.add_argument("--fleet", help="fleet name (default: first in fleets.yaml)")
    fleet_soul_pull.add_argument("--into", required=True, help="destination directory for the snapshot")
    fleet_soul_pull.add_argument(
        "--fleets-config", default=str(mac_paths.fleets_config())
    )
    fleet_soul_pull.add_argument(
        "--memory-checksum", action="store_true",
        help="also sha256 the binary memory blobs (reads them remotely; slower)",
    )
    fleet_soul_pull.add_argument(
        "--with-hub", action="store_true",
        help="also capture hub-stored persona + current mood per agent (needs hub access)",
    )
    _set(cmd_fleet_soul_pull, fleet_soul_pull)

    fleet_soul_push = fleet.add_parser(
        "soul-push",
        help="diff an edited soul snapshot vs live and write changes (backup-before-replace)",
    )
    fleet_soul_push.add_argument("--from", dest="from_dir", required=True, help="snapshot directory")
    fleet_soul_push.add_argument(
        "--dry-run", action="store_true", help="show the plan; write nothing (default off)"
    )
    fleet_soul_push.add_argument(
        "--agent", action="append", help="limit to this agent (repeatable)"
    )
    fleet_soul_push.add_argument("--fleet", help="fleet name (default: snapshot manifest)")
    fleet_soul_push.add_argument(
        "--fleets-config", default=str(mac_paths.fleets_config())
    )
    _set(cmd_fleet_soul_push, fleet_soul_push)

    fleet_soul_audit = fleet.add_parser(
        "soul-audit",
        help="audit the remote ~/.hermes directory for a named agent",
    )
    fleet_soul_audit.add_argument("--agent", required=True, help="agent name to audit")
    fleet_soul_audit.add_argument("--fleet", help="fleet name (default: first in fleets.yaml)")
    fleet_soul_audit.add_argument(
        "--fleets-config", default=str(mac_paths.fleets_config())
    )
    _set(cmd_fleet_soul_audit, fleet_soul_audit)


    # Phase 2b: export/vet the fleet's Qdrant vector memory.
    fleet_mem_export = fleet.add_parser(
        "memory-export",
        help="export Qdrant vector memory to greppable JSONL for vetting",
    )
    fleet_mem_export.add_argument("--qdrant-url", required=True, help="e.g. http://100.125.137.89:6333")
    fleet_mem_export.add_argument("--agent", help="filter to this agent_id")
    fleet_mem_export.add_argument("--collections", help="CSV of collections (default: mac_memory_medium,mac_memory_long)")
    fleet_mem_export.add_argument("--search", help="case-insensitive substring filter (e.g. a stale name)")
    fleet_mem_export.add_argument("--into", help="write JSONL here (default: stdout)")
    _set(cmd_fleet_memory_export, fleet_mem_export)

    fleet_mem_prune = fleet.add_parser(
        "memory-prune",
        help="DELETE vetted Qdrant point ids from a collection (destructive)",
    )
    fleet_mem_prune.add_argument("--qdrant-url", required=True)
    fleet_mem_prune.add_argument("--collection", required=True)
    fleet_mem_prune.add_argument("--id", action="append", help="point id to delete (repeatable)")
    fleet_mem_prune.add_argument("--from-jsonl", help="delete the ids in this memory-export JSONL")
    _set(cmd_fleet_memory_prune, fleet_mem_prune)

    fleet_refresh = fleet.add_parser(
        "refresh-context",
        help="refresh the live Fleet section in this agent's runtime-context markdown "
        "(what the nap-tick-style timer calls so each session knows its teammates)",
    )
    fleet_refresh.add_argument("--agent", help="this agent's id (excluded from its own fleet view)")
    fleet_refresh.add_argument(
        "--markdown",
        help="runtime-context markdown path (default: $MAC_HERMES_RUNTIME_CONTEXT_MARKDOWN "
        "or ~/.hermes/mac-runtime-context.md)",
    )
    _set(cmd_fleet_refresh_context, fleet_refresh)

    fleet_validate = fleet.add_parser(
        "validate",
        help="validate a declarative mac.fleet_setup.v1 setup spec",
    )
    fleet_validate.add_argument("--spec", required=True)
    fleet_validate.add_argument(
        "--fleets-config",
        default=str(mac_paths.fleets_config()),
    )
    fleet_validate.add_argument(
        "--env-file",
        default=str(mac_paths.deploy_env_file()),
    )
    _set(cmd_fleet_validate_setup, fleet_validate)

    # journal-01: daily snapshots of an agent's soul + memory state so an
    # evolved personality can be restored if its files are ever lost. Local
    # file ops only — no hub/--db needed.
    journal = sub.add_parser(
        "journal",
        help="snapshot / restore an agent's soul + memory state (guards against soul loss)",
    ).add_subparsers(dest="journal_command", required=True)

    journal_snap = journal.add_parser(
        "snapshot",
        help="snapshot SOUL/USER/MEMORY/memories/mood/config to $HOME/.mac/journal/<date>/ "
        "and run MAC_JOURNAL_BACKUP_HOOK if set",
    )
    journal_snap.add_argument("--dir", help="journal root (default $MAC_JOURNAL_DIR or ~/.mac/journal)")
    journal_snap.add_argument("--home", help="agent HERMES_HOME (default $HERMES_HOME or ~/.hermes)")
    journal_snap.add_argument("--date", help="snapshot date label (default today, UTC)")
    journal_snap.add_argument("--agent", help="agent id label (default $MAC_AGENT_ID)")
    journal_snap.add_argument("--no-hook", action="store_true", help="skip the backup hook")
    _set(cmd_journal_snapshot, journal_snap)

    journal_list = journal.add_parser("list", help="list journaled snapshots")
    journal_list.add_argument("--dir", help="journal root (default ~/.mac/journal)")
    _set(cmd_journal_list, journal_list)

    journal_restore = journal.add_parser(
        "restore",
        help="restore an agent's state from a journal date (snapshots current state first, so it's reversible)",
    )
    journal_restore.add_argument("date", help="journal date to restore (e.g. 2026-06-04)")
    journal_restore.add_argument("--dir", help="journal root (default ~/.mac/journal)")
    journal_restore.add_argument("--home", help="agent HERMES_HOME to restore into")
    journal_restore.add_argument("--dry-run", action="store_true", help="show what would be restored, change nothing")
    _set(cmd_journal_restore, journal_restore)

    fleet_doctor = fleet.add_parser(
        "doctor",
        help="run setup doctor checks for a declarative fleet spec",
    )
    fleet_doctor.add_argument("--spec", required=True)
    fleet_doctor.add_argument(
        "--fleets-config",
        default=str(mac_paths.fleets_config()),
    )
    fleet_doctor.add_argument(
        "--env-file",
        default=str(mac_paths.deploy_env_file()),
    )
    _set(cmd_fleet_doctor_setup, fleet_doctor)

    # auth-token-sync-01: recover/re-sync a client's bearer token from the hub.
    fleet_sync_token = fleet.add_parser(
        "sync-token",
        help="pull the hub's current MAC_API_TOKEN into ~/.mac/.env as "
        "MAC_API_TOKEN__<FLEET> (fixes 403 'unknown bearer token' drift)",
    )
    fleet_sync_token.add_argument(
        "--fleet",
        required=True,
        help="fleet name to sync (resolves the hub's ssh target from fleets.yaml)",
    )
    fleet_sync_token.add_argument(
        "--fleets-config",
        default=str(mac_paths.fleets_config()),
        help="path to fleets.yaml (default ~/.mac/fleets.yaml)",
    )
    fleet_sync_token.add_argument(
        "--env-file",
        default=str(mac_paths.deploy_env_file()),
        help="client env file to update (default ~/.mac/.env)",
    )
    _set(cmd_fleet_sync_token, fleet_sync_token)

    # Coding-CLI credential fabric: the operator's CURRENT workstation is the
    # source of truth for claude/codex/cursor auth; workers get it over the
    # fleet's SSH routes, on demand.
    fleet_creds_status = fleet.add_parser(
        "creds-status",
        help="per-agent coding-CLI (claude/codex/cursor) auth status, from the "
        "agents' own heartbeat reports; flags who needs a credential sync",
    )
    # Hub selection comes from the GLOBAL --fleet/--hub-url options (a
    # subparser --fleet default would clobber the parsed global value).
    _set(cmd_fleet_creds_status, fleet_creds_status)

    fleet_creds_sync = fleet.add_parser(
        "creds-sync",
        help="push THIS workstation's coding-CLI credentials to fleet workers "
        "over their SSH routes (stdin-only transfer; verified on arrival)",
    )
    fleet_creds_sync.add_argument(
        "--fleet",
        dest="creds_fleet",
        required=True,
        help="fleet name (resolves SSH routes from fleets.yaml); a distinct "
        "dest so it cannot clobber the global --fleet authority selection",
    )
    fleet_creds_sync.add_argument(
        "--agent",
        action="append",
        default=None,
        help="target agent name; repeatable. Default: every agent that "
        "reported a CLI on PATH without auth (same set --needed selects)",
    )
    fleet_creds_sync.add_argument(
        "--cli",
        default="claude,codex,cursor",
        help="comma-separated CLIs to sync (default: claude,codex,cursor)",
    )
    fleet_creds_sync.add_argument(
        "--fleets-config",
        default=str(mac_paths.fleets_config()),
        help="path to fleets.yaml (default ~/.mac/fleets.yaml)",
    )
    fleet_creds_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be synced where, without moving any secret",
    )
    _set(cmd_fleet_creds_sync, fleet_creds_sync)

    # mac-ghingest: GitHub issues as an asynchronous work generator. Poll
    # opted-in repos and file idempotent mac tasks; observe/trigger it and opt
    # projects in/out from here.
    fleet_ghingest = fleet.add_parser(
        "github-ingest",
        help="GitHub-issue work generator: status, manual run, and per-project opt-in",
    )
    ghingest_sub = fleet_ghingest.add_subparsers(dest="github_ingest_command")
    ghingest_sub.required = True

    ghingest_status = ghingest_sub.add_parser(
        "status", help="show ingestor config + last run report (hub read)"
    )
    _set(cmd_fleet_github_ingest_status, ghingest_status)

    ghingest_run = ghingest_sub.add_parser(
        "run", help="trigger one immediate ingestion pass across opted-in repos"
    )
    _set(cmd_fleet_github_ingest_run, ghingest_run)

    ghingest_enable = ghingest_sub.add_parser(
        "enable", help="opt a project into GitHub-issue ingestion"
    )
    ghingest_enable.add_argument("project", help="project name (must be onboarded)")
    ghingest_enable.add_argument(
        "--label",
        action="append",
        default=None,
        help="only ingest issues carrying this label; repeatable (default: all open issues)",
    )
    ghingest_enable.add_argument(
        "--capability",
        action="append",
        default=None,
        help="required capability to stamp on created tasks; repeatable",
    )
    ghingest_enable.add_argument(
        "--auto-cancel-closed",
        action="store_true",
        help="cancel the OPEN task for an issue when the issue closes on GitHub",
    )
    _set(cmd_project_ingest_enable, ghingest_enable)

    ghingest_disable = ghingest_sub.add_parser(
        "disable", help="opt a project out of GitHub-issue ingestion"
    )
    ghingest_disable.add_argument("project", help="project name")
    _set(cmd_project_ingest_disable, ghingest_disable)

    # auth-token-sync-01: graceful rotation via the overlapping MAC_API_TOKENS map.
    fleet_rotate_token = fleet.add_parser(
        "rotate-token",
        help="rotate the hub bearer token with an overlap window (dry-run unless --apply)",
    )
    fleet_rotate_token.add_argument(
        "--fleet",
        required=True,
        help="fleet name to rotate",
    )
    fleet_rotate_token.add_argument(
        "--scope",
        action="append",
        help="scope for the new token (repeatable; default admin)",
    )
    fleet_rotate_token.add_argument(
        "--prune",
        action="store_true",
        help="end the overlap: drop all but the current token (run after every "
        "client has synced to the new token)",
    )
    fleet_rotate_token.add_argument(
        "--apply",
        action="store_true",
        help="actually mutate the hub + this client (default: dry-run plan only)",
    )
    fleet_rotate_token.add_argument(
        "--restart",
        action="store_true",
        help="with --apply, also run the hub restart command over SSH",
    )
    fleet_rotate_token.add_argument(
        "--fleets-config",
        default=str(mac_paths.fleets_config()),
        help="path to fleets.yaml (default ~/.mac/fleets.yaml)",
    )
    fleet_rotate_token.add_argument(
        "--env-file",
        default=str(mac_paths.deploy_env_file()),
        help="client env file to update (default ~/.mac/.env)",
    )
    _set(cmd_fleet_rotate_token, fleet_rotate_token)

    fleet_move = fleet.add_parser(
        "move-agent",
        help=(
            "move an agent between fleets: rewrite fleets.yaml entry, "
            "print redeploy command, and emit DB reconcile commands. "
            "Dry-run by default; pass --execute to mutate fleets.yaml."
        ),
    )
    fleet_move.add_argument(
        "--agent",
        required=True,
        help="agent name to move (e.g. worker-1)",
    )
    fleet_move.add_argument(
        "--from",
        dest="from_fleet",
        default=None,
        help="source fleet hub-name (default: auto-detect from fleets.yaml)",
    )
    fleet_move.add_argument(
        "--to",
        dest="to_fleet",
        required=True,
        help="target fleet hub-name",
    )
    fleet_move.add_argument(
        "--to-os",
        default="linux",
        choices=["linux", "darwin"],
        help="OS of the agent on the destination (default: linux)",
    )
    fleet_move.add_argument(
        "--hub-url",
        default="",
        help=(
            "override the hub_url written into the agent entry "
            "(default: inherit from target fleet)"
        ),
    )
    fleet_move.add_argument(
        "--no-db-reconcile",
        action="store_true",
        help="skip the DB fleet-membership reconcile note",
    )
    fleet_move.add_argument(
        "--no-redeploy",
        action="store_true",
        help=(
            "with --execute, only rewrite fleets.yaml and EMIT the redeploy "
            "command instead of running it (inspect-first; default is to run "
            "the redeploy end-to-end)"
        ),
    )
    fleet_move.add_argument(
        "--execute",
        action="store_true",
        help="actually mutate fleets.yaml + run the redeploy (default: dry-run plan only)",
    )
    _set(cmd_fleet_move_agent, fleet_move)

    optimizer = sub.add_parser(
        "optimizer",
        help="autonomous scientific policy optimization",
        description=(
            "Create allowlisted execution policies, run controlled task experiments, "
            "and promote only statistically superior, quality-noninferior treatments."
        ),
    ).add_subparsers(dest="optimizer_command", required=True)
    _set(cmd_optimizer_status, optimizer.add_parser("status", help="show scheduler and active experiments"))
    _set(cmd_optimizer_tick, optimizer.add_parser("tick", help="run one observation, decision, and hypothesis pass now"))

    optimizer_policy = optimizer.add_parser(
        "policy", help="versioned execution-policy lifecycle"
    ).add_subparsers(dest="optimizer_policy_command", required=True)
    optimizer_policy_create = optimizer_policy.add_parser(
        "create", help="create a candidate allowlisted policy"
    )
    optimizer_policy_create.add_argument("name")
    optimizer_policy_create.add_argument("project")
    optimizer_policy_create.add_argument("--parameters")
    optimizer_policy_create.add_argument("--parameters-file")
    optimizer_policy_create.add_argument("--description")
    optimizer_policy_create.add_argument("--description-file")
    optimizer_policy_create.add_argument("--actor", default="human")
    _set(cmd_optimizer_policy_create, optimizer_policy_create)
    optimizer_policy_list = optimizer_policy.add_parser("list", help="list policies")
    optimizer_policy_list.add_argument("--project")
    optimizer_policy_list.add_argument(
        "--status", choices=("candidate", "active", "retired")
    )
    _set(cmd_optimizer_policy_list, optimizer_policy_list)
    optimizer_policy_show = optimizer_policy.add_parser("show", help="show one policy")
    optimizer_policy_show.add_argument("policy_id")
    _set(cmd_optimizer_policy_show, optimizer_policy_show)
    optimizer_policy_promote = optimizer_policy.add_parser(
        "promote", help="make a policy active"
    )
    optimizer_policy_promote.add_argument("policy_id")
    optimizer_policy_promote.add_argument("--actor", default="operator")
    optimizer_policy_promote.add_argument("--reason")
    optimizer_policy_promote.add_argument("--reason-file")
    _set(cmd_optimizer_policy_promote, optimizer_policy_promote)
    optimizer_policy_rollback = optimizer_policy.add_parser(
        "rollback", help="restore a prior policy as active"
    )
    optimizer_policy_rollback.add_argument("project")
    optimizer_policy_rollback.add_argument("policy_id")
    optimizer_policy_rollback.add_argument("--actor", default="operator")
    optimizer_policy_rollback.add_argument("--reason")
    optimizer_policy_rollback.add_argument("--reason-file")
    _set(cmd_optimizer_policy_rollback, optimizer_policy_rollback)

    optimizer_experiment = optimizer.add_parser(
        "experiment", help="controlled experiment lifecycle"
    ).add_subparsers(dest="optimizer_experiment_command", required=True)
    optimizer_experiment_create = optimizer_experiment.add_parser(
        "create", help="register a hypothesis and A/B protocol"
    )
    optimizer_experiment_create.add_argument("name")
    optimizer_experiment_create.add_argument("project")
    optimizer_experiment_create.add_argument("control_policy_id")
    optimizer_experiment_create.add_argument("treatment_policy_id")
    optimizer_experiment_create.add_argument("--hypothesis")
    optimizer_experiment_create.add_argument("--hypothesis-file")
    optimizer_experiment_create.add_argument(
        "--primary-metric",
        required=True,
        choices=(
            "accepted_success",
            "delayed_quality_success",
            "cycles_to_accept",
            "executor_attempts",
            "review_attempts",
            "lead_time_ms",
            "model_latency_ms",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "cost_usd",
            "escaped_defect_severity",
        ),
    )
    optimizer_experiment_create.add_argument(
        "--direction", choices=("maximize", "minimize")
    )
    optimizer_experiment_create.add_argument("--min-effect", type=float, default=0.0)
    optimizer_experiment_create.add_argument("--quality-margin", type=float, default=0.05)
    optimizer_experiment_create.add_argument("--min-samples-per-arm", type=int)
    optimizer_experiment_create.add_argument("--max-samples-per-arm", type=int)
    optimizer_experiment_create.add_argument("--exploration-fraction", type=float)
    optimizer_experiment_create.add_argument("--outcome-horizon-seconds", type=float)
    optimizer_experiment_create.add_argument("--guardrails")
    optimizer_experiment_create.add_argument("--guardrails-file")
    optimizer_experiment_create.add_argument("--metadata")
    optimizer_experiment_create.add_argument("--metadata-file")
    optimizer_experiment_create.add_argument(
        "--auto-promote",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="automatically promote an evidence-backed winner (default: service config)",
    )
    optimizer_experiment_create.add_argument("--actor", default="human")
    _set(cmd_optimizer_experiment_create, optimizer_experiment_create)
    optimizer_experiment_list = optimizer_experiment.add_parser(
        "list", help="list experiments"
    )
    optimizer_experiment_list.add_argument("--project")
    optimizer_experiment_list.add_argument(
        "--state",
        choices=(
            "draft",
            "running",
            "candidate",
            "monitoring",
            "paused",
            "completed",
            "rejected",
            "rolled_back",
        ),
    )
    _set(cmd_optimizer_experiment_list, optimizer_experiment_list)
    optimizer_experiment_show = optimizer_experiment.add_parser(
        "show", help="show one experiment"
    )
    optimizer_experiment_show.add_argument("experiment_id")
    _set(cmd_optimizer_experiment_show, optimizer_experiment_show)
    optimizer_experiment_evidence = optimizer_experiment.add_parser(
        "evidence", help="show assignments, KPI observations, decisions, and events"
    )
    optimizer_experiment_evidence.add_argument("experiment_id")
    optimizer_experiment_evidence.add_argument("--limit", type=int, default=500)
    _set(cmd_optimizer_experiment_evidence, optimizer_experiment_evidence)
    optimizer_experiment_start = optimizer_experiment.add_parser(
        "start", help="start task assignment"
    )
    optimizer_experiment_start.add_argument("experiment_id")
    optimizer_experiment_start.add_argument("--actor", default="operator")
    _set(cmd_optimizer_experiment_start, optimizer_experiment_start)
    for action, handler, help_text in (
        ("pause", cmd_optimizer_experiment_pause, "pause assignment and release the project slot"),
        ("promote", cmd_optimizer_experiment_promote, "promote an evidence-backed candidate"),
    ):
        action_parser = optimizer_experiment.add_parser(action, help=help_text)
        action_parser.add_argument("experiment_id")
        action_parser.add_argument("--actor", default="operator")
        action_parser.add_argument("--reason")
        action_parser.add_argument("--reason-file")
        _set(handler, action_parser)
    optimizer_experiment_observe = optimizer_experiment.add_parser(
        "observe", help="refresh one assigned task's KPI projection"
    )
    optimizer_experiment_observe.add_argument("experiment_id")
    optimizer_experiment_observe.add_argument("task_id")
    _set(cmd_optimizer_experiment_observe, optimizer_experiment_observe)
    optimizer_experiment_analyze = optimizer_experiment.add_parser(
        "analyze", help="refresh all assigned tasks and evaluate the protocol"
    )
    optimizer_experiment_analyze.add_argument("experiment_id")
    _set(cmd_optimizer_experiment_analyze, optimizer_experiment_analyze)

    mood = sub.add_parser(
        "mood",
        help="agent mood overlays (agents self-report; operators query)",
    ).add_subparsers(dest="mood_command", required=True)
    mood_set = mood.add_parser("set", help="record a mood transition")
    mood_set.add_argument("agent_id")
    mood_set.add_argument(
        "mode",
        choices=(
            "warm",
            "cheerful",
            "sad",
            "curt",
            "cold",
            "irritated",
            "angry",
            "enraged",
        ),
    )
    mood_set.add_argument("--set-by", help="actor (defaults to agent_id)")
    mood_set.add_argument("--reason", help="why the agent picked this mode")
    mood_set.add_argument("--ttl-seconds", type=int)
    mood_set.add_argument("--metadata")
    _set(cmd_mood_set, mood_set)
    mood_show = mood.add_parser("show", help="current mood for an agent")
    mood_show.add_argument("agent_id")
    _set(cmd_mood_show, mood_show)
    mood_clear = mood.add_parser("clear", help="end the active overlay")
    mood_clear.add_argument("agent_id")
    mood_clear.add_argument("--cleared-by")
    mood_clear.add_argument("--reason")
    _set(cmd_mood_clear, mood_clear)
    mood_history = mood.add_parser("history", help="mood transitions for an agent")
    mood_history.add_argument("agent_id")
    mood_history.add_argument("--limit", type=int, default=50)
    _set(cmd_mood_history, mood_history)

    dream = sub.add_parser(
        "dream",
        help="dream-cycle learning maintenance",
    ).add_subparsers(dest="dream_command", required=True)

    dream_run = dream.add_parser(
        "run", help="curate memory into a reviewable candidate store"
    )
    dream_run.add_argument("--agent-id", default=None)
    dream_run.add_argument("--project", default=None)
    dream_run.add_argument(
        "--since", default="", help="only read memory written after this timestamp"
    )
    dream_run.add_argument("--limit", type=int, default=2000)
    dream_run.add_argument(
        "--instructions", default="", help="steer what the dream synthesises"
    )
    dream_run.add_argument(
        "--max-output-ratio",
        type=float,
        default=0.75,
        help="quarantine the run if output/input exceeds this (default 0.75)",
    )
    dream_run.add_argument(
        "--promote",
        action="store_true",
        help="adopt immediately if every gate passes (default: review first)",
    )
    _set(cmd_dream_run, dream_run)

    dream_list = dream.add_parser("list", help="list dream runs, newest first")
    dream_list.add_argument("--state", default=None)
    dream_list.add_argument("--limit", type=int, default=20)
    _set(cmd_dream_list, dream_list)

    dream_show = dream.add_parser("show", help="show a run, its gates and candidates")
    dream_show.add_argument("run_id")
    _set(cmd_dream_show, dream_show)

    dream_promote = dream.add_parser(
        "promote", help="adopt a reviewed run into live memory"
    )
    dream_promote.add_argument("run_id")
    dream_promote.add_argument(
        "--keep-superseded",
        action="store_true",
        help="do not retire superseded rows (the store will not shrink)",
    )
    _set(cmd_dream_promote, dream_promote)

    dream_discard = dream.add_parser("discard", help="discard a dream run")
    dream_discard.add_argument("run_id")
    dream_discard.add_argument("--reason", default="")
    _set(cmd_dream_discard, dream_discard)

    dream_import = dream.add_parser(
        "import-logs",
        help="merge orphaned ~/.hermes/dream_logs reports into durable memory",
    )
    dream_import.add_argument(
        "--dream-logs-dir",
        default=None,
        help="source dir (default: $HERMES_HOME/dream_logs)",
    )
    dream_import.add_argument(
        "--agent-id", default=None, help="attribute imported dreams to this agent"
    )
    dream_import.add_argument("--qdrant-url", default=None)
    dream_import.add_argument(
        "--no-embed",
        action="store_true",
        help="skip vector embedding (imported dreams won't be recall-eligible)",
    )
    dream_import.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be imported without writing",
    )
    _set(cmd_dream_import_logs, dream_import)

    nap = sub.add_parser(
        "nap",
        help="agent nap schedule and lifecycle (daily memory consolidation)",
    ).add_subparsers(dest="nap_command", required=True)
    nap_configure = nap.add_parser(
        "configure",
        help="set or refresh an agent's nap schedule (offset defaults to a deterministic hash of agent.name)",
    )
    nap_configure.add_argument("agent_id")
    nap_configure.add_argument(
        "--offset-minutes",
        type=int,
        help="0-359; omit to derive deterministically from agent name",
    )
    nap_configure.add_argument("--window-minutes", type=int, default=15)
    nap_configure.add_argument("--disabled", action="store_true")
    nap_configure.add_argument("--actor")
    _set(cmd_nap_configure, nap_configure)
    nap_show = nap.add_parser("show")
    nap_show.add_argument("agent_id")
    _set(cmd_nap_show, nap_show)
    nap_next = nap.add_parser("next", help="compute the next nap window")
    nap_next.add_argument("agent_id")
    _set(cmd_nap_next, nap_next)
    nap_begin = nap.add_parser(
        "begin",
        help="start a nap; transitions the agent to DRAINING",
    )
    nap_begin.add_argument("agent_id")
    nap_begin.add_argument("--actor")
    nap_begin.add_argument("--detail")
    _set(cmd_nap_begin, nap_begin)
    nap_complete = nap.add_parser(
        "complete",
        help="mark a nap_run completed and restore the agent",
    )
    nap_complete.add_argument("run_id")
    nap_complete.add_argument(
        "--evidence-id",
        help="evidence row (kind='log') with the summary artifact pointer",
    )
    nap_complete.add_argument("--detail")
    nap_complete.add_argument("--actor")
    _set(cmd_nap_complete, nap_complete)
    nap_fail = nap.add_parser("fail", help="mark a nap_run failed and restore the agent")
    nap_fail.add_argument("run_id")
    nap_fail.add_argument("--reason", required=True)
    nap_fail.add_argument("--actor")
    _set(cmd_nap_fail, nap_fail)
    nap_list = nap.add_parser("list", help="list nap_runs")
    nap_list.add_argument("--agent-id")
    _set(cmd_nap_list, nap_list)

    # mem-08 autonomy: run the whole begin → consolidate → complete arc.
    nap_cycle = nap.add_parser(
        "cycle",
        help="run a full nap cycle (begin + consolidate + complete) for "
        "one agent — what the auto-trigger timer calls",
    )
    nap_cycle.add_argument("agent_id")
    nap_cycle.add_argument("--actor")
    nap_cycle.add_argument("--no-embed", action="store_true")
    nap_cycle.add_argument("--no-dreams", action="store_true")
    nap_cycle.add_argument("--qdrant-url")
    _set(cmd_nap_cycle, nap_cycle)

    nap_due = nap.add_parser(
        "due",
        help="list enabled nap_schedules whose current window has opened "
        "and not yet been completed",
    )
    nap_due.add_argument(
        "--as-of",
        help="ISO timestamp to compute due-ness against (default: now)",
    )
    nap_due.add_argument(
        "--format",
        choices=("json", "agent-ids"),
        default="json",
        help="'json' (full due list) or 'agent-ids' (newline-delimited "
        "agent_ids, for piping to `xargs mac admin nap cycle`)",
    )
    _set(cmd_nap_due, nap_due)

    # mem-08: build per-(task/project) memory summaries for an agent.
    nap_consolidate = nap.add_parser(
        "consolidate",
        help="walk the agent's recent memory_records, summarize by "
        "task/project, write a nap_summary row per group, and embed "
        "into the medium tier (mem-08)",
    )
    nap_consolidate.add_argument("agent_id")
    nap_consolidate.add_argument(
        "--since",
        help="ISO timestamp lower bound; default = last successful nap's "
        "completed_at, or '<beginning>' if there isn't one",
    )
    nap_consolidate.add_argument("--nap-run-id", help="link the summaries to this run")
    nap_consolidate.add_argument("--created-by")
    nap_consolidate.add_argument(
        "--no-embed",
        action="store_true",
        help="skip the vector-writer handoff (summary-only mode; useful "
        "when Qdrant is offline)",
    )
    nap_consolidate.add_argument(
        "--no-dreams",
        action="store_true",
        help="write nap_summary rows only; skip typed mac.dream.v1 artifacts",
    )
    nap_consolidate.add_argument(
        "--qdrant-url",
        help="override the default Qdrant URL passed to the vector writer",
    )
    _set(cmd_nap_consolidate, nap_consolidate)

    dispatch = sub.add_parser("dispatch", help="dispatcher commands").add_subparsers(dest="dispatch_command", required=True)
    assign = dispatch.add_parser("assign")
    assign.add_argument("--lease-seconds", type=int, default=900)
    _set(cmd_dispatch_once, assign)
    tick = dispatch.add_parser("tick")
    tick.add_argument("--lease-seconds", type=int, default=900)
    tick.add_argument("--limit", type=int, default=100)
    _set(cmd_dispatch_tick, tick)

    message = sub.add_parser("message", help="structured message bus commands").add_subparsers(dest="message_command", required=True)
    send = message.add_parser("send")
    send.add_argument("sender_agent_id")
    send.add_argument("--recipient-agent-id")
    send.add_argument("--task-id")
    send.add_argument("--message-type", required=True)
    send.add_argument("--payload", required=True)
    _set(cmd_message_send, send)
    inbox = message.add_parser("inbox")
    inbox.add_argument("agent_id")
    inbox.add_argument("--limit", type=int, default=50)
    _set(cmd_message_inbox, inbox)

    agentbus = sub.add_parser(
        "agentbus",
        help="typed high-throughput agent-to-agent content streams",
    ).add_subparsers(dest="agentbus_command", required=True)
    bus_open = agentbus.add_parser("open")
    bus_open.add_argument("sender_agent_id")
    bus_open.add_argument("--recipient-agent-id")
    bus_open.add_argument("--task-id")
    bus_open.add_argument("--topic", default="content")
    bus_open.add_argument("--content-type", default="application/json")
    bus_open.add_argument("--headers")
    bus_open.add_argument("--stream-id")
    _set(cmd_agentbus_open, bus_open)

    bus_append = agentbus.add_parser("append")
    bus_append.add_argument("stream_id")
    bus_append.add_argument("sender_agent_id")
    bus_append.add_argument("--payload")
    bus_append.add_argument("--content-type")
    bus_append.add_argument(
        "--payload-encoding",
        choices=("json", "text", "base64"),
        default="json",
    )
    bus_append.add_argument("--final", action="store_true")
    _set(cmd_agentbus_append, bus_append)

    bus_close = agentbus.add_parser("close")
    bus_close.add_argument("stream_id")
    bus_close.add_argument("sender_agent_id")
    bus_close.add_argument("--status", choices=("closed", "aborted"), default="closed")
    _set(cmd_agentbus_close, bus_close)

    bus_list = agentbus.add_parser("list")
    bus_list.add_argument("--agent-id")
    bus_list.add_argument("--status", choices=("open", "closed", "aborted"))
    bus_list.add_argument("--limit", type=int, default=100)
    _set(cmd_agentbus_list, bus_list)

    bus_wait = agentbus.add_parser(
        "wait",
        help="block until this agent is messaged, then print and exit",
        description=(
            "Run as a BACKGROUND task of a working agent's harness. The agent "
            "keeps working in the foreground; the harness surfaces this between "
            "steps, so a correction reaches the agent mid-task instead of after "
            "it finishes. Exits on the first message -- restart it after acting, "
            "passing --after-cursor from the previous run so nothing is missed."
        ),
    )
    bus_wait.add_argument("agent_id")
    bus_wait.add_argument(
        "--after-cursor",
        default="",
        help="next_cursor from the previous wait; resumes exactly where it stopped",
    )
    bus_wait.add_argument("--timeout-seconds", type=float, default=300.0)
    bus_wait.add_argument("--poll-interval-seconds", type=float, default=1.0)
    bus_wait.add_argument("--limit", type=int, default=100)
    _set(cmd_agentbus_wait, bus_wait)

    bus_read = agentbus.add_parser("read")
    bus_read.add_argument("stream_id")
    bus_read.add_argument("agent_id")
    bus_read.add_argument("--after-sequence", type=int, default=0)
    bus_read.add_argument("--limit", type=int, default=100)
    _set(cmd_agentbus_read, bus_read)

    bus_publish = agentbus.add_parser("publish")
    bus_publish.add_argument("sender_agent_id")
    bus_publish.add_argument("--recipient-agent-id")
    bus_publish.add_argument("--task-id")
    bus_publish.add_argument("--topic", default="content")
    bus_publish.add_argument("--content-type", default="application/json")
    bus_publish.add_argument("--headers")
    bus_publish.add_argument("--payload")
    bus_publish.add_argument(
        "--payload-encoding",
        choices=("json", "text", "base64"),
        default="json",
    )
    _set(cmd_agentbus_publish, bus_publish)

    bus_repo_update = agentbus.add_parser("repo-update")
    bus_repo_update.add_argument("sender_agent_id")
    bus_repo_update.add_argument("--recipient-agent-id", action="append")
    bus_repo_update.add_argument("--all-agents", action="store_true")
    bus_repo_update.add_argument("--repo-path")
    bus_repo_update.add_argument("--remote", default="origin")
    bus_repo_update.add_argument("--branch", default="main")
    bus_repo_update.add_argument("--request-id")
    bus_repo_update.add_argument("--no-restart", action="store_true")
    bus_repo_update.add_argument("--restart-service", action="append")
    _set(cmd_agentbus_repo_update, bus_repo_update)

    bus_artifact_publish = agentbus.add_parser("artifact-publish")
    bus_artifact_publish.add_argument("sender_agent_id")
    bus_artifact_publish.add_argument(
        "--operation",
        choices=("create", "upsert", "update", "get", "read", "list", "delete"),
        default="upsert",
    )
    bus_artifact_publish.add_argument("--recipient-agent-id", action="append")
    bus_artifact_publish.add_argument("--all-agents", action="store_true")
    bus_artifact_publish.add_argument("--artifact", help="artifact id for get/delete")
    bus_artifact_publish.add_argument("--digest")
    bus_artifact_publish.add_argument("--kind", default="public-artifact")
    bus_artifact_publish.add_argument("--uri")
    bus_artifact_publish.add_argument("--public-url")
    bus_artifact_publish.add_argument("--path", help="path under MAC_PUBLISH_DIR/public URL")
    bus_artifact_publish.add_argument("--publish-dir")
    bus_artifact_publish.add_argument("--sbom-uri")
    bus_artifact_publish.add_argument("--signers", help="comma-separated signer identities")
    bus_artifact_publish.add_argument("--metadata")
    bus_artifact_publish.add_argument("--task-id")
    bus_artifact_publish.add_argument("--request-id")
    _set(cmd_agentbus_artifact_publish, bus_artifact_publish)

    review = sub.add_parser("review", help="review pipeline commands").add_subparsers(dest="review_command", required=True)
    request = review.add_parser("request")
    request.add_argument("task_id")
    request.add_argument("reviewer_agent_id")
    request.add_argument("--actor", default="dispatcher")
    _set(cmd_review_request, request)
    decision = review.add_parser("decision")
    decision.add_argument("review_id")
    decision.add_argument("status")
    decision.add_argument("reviewer_agent_id")
    decision.add_argument("--reason")
    decision.add_argument("--evidence-id")
    _set(cmd_review_decision, decision)

    auto_land = review.add_parser(
        "auto-land",
        help=(
            "land a task/branch iff the contract gate is GREEN and an "
            "independent adversarial reviewer APPROVEs (default-to-reject)"
        ),
    )
    auto_land.add_argument("target", help="task id or branch/ref to auto-land")
    auto_land.add_argument(
        "--repo-dir", default=".", help="repository directory (default: cwd)"
    )
    auto_land.add_argument(
        "--base-ref",
        default="main",
        help="base ref for the land-time merge-gate check (default: main)",
    )
    auto_land.add_argument("--created-by", default="auto-land")
    auto_land.add_argument(
        "--author",
        default="",
        help=(
            "the change author's fleet agent id; the adversarial reviewer must "
            "be a DIFFERENT agent (independence). Defaults to $MAC_AGENT_ID."
        ),
    )
    auto_land.add_argument(
        "--push",
        action="store_true",
        help=(
            "on a branch target, perform a plain (never --force) git push after "
            "both gates pass; off by default (only marks ready-to-land)"
        ),
    )
    auto_land.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the plan (target + config + which gates would run) without "
            "running the contract gate, spawning a reviewer, or landing anything"
        ),
    )
    _set(cmd_review_auto_land, auto_land)

    experiment = review.add_parser(
        "experiment",
        help="assign and inspect replayable review-strategy experiments",
    ).add_subparsers(dest="review_experiment_command", required=True)
    experiment_assign = experiment.add_parser(
        "assign",
        help="persist an explicit or deterministic weighted arm assignment",
    )
    experiment_assign.add_argument("task_id")
    experiment_assign.add_argument("experiment_id")
    assignment = experiment_assign.add_mutually_exclusive_group(required=True)
    assignment.add_argument("--arm", help="explicit arm name")
    assignment.add_argument(
        "--arms",
        help="deterministic weighted assignment, e.g. blind=1,standard=1",
    )
    experiment_assign.add_argument(
        "--probability",
        type=float,
        help="propensity for an explicit --arm (defaults to 1)",
    )
    experiment_assign.add_argument("--blind", action="store_true")
    experiment_assign.add_argument(
        "--blind-arm",
        action="append",
        default=[],
        help="weighted arm that uses evidence-withheld discovery (repeatable)",
    )
    experiment_assign.add_argument("--policy-version", default="v1")
    experiment_assign.add_argument("--hypothesis")
    experiment_assign.add_argument("--hypothesis-file")
    experiment_assign.add_argument("--stratum", default="")
    experiment_assign.add_argument("--actor", default="human")
    _set(cmd_review_experiment_assign, experiment_assign)

    experiment_observe = experiment.add_parser(
        "observe",
        help="derive one task observation from ledger evidence",
    )
    experiment_observe.add_argument("task_id")
    _set(cmd_review_experiment_observe, experiment_observe)

    experiment_outcome = experiment.add_parser(
        "outcome",
        help="append an operator or delayed validation outcome",
    )
    experiment_outcome.add_argument("task_id")
    experiment_outcome.add_argument("kind")
    experiment_outcome.add_argument(
        "status", choices=("confirmed", "refuted", "pending")
    )
    experiment_outcome.add_argument("--finding-id", default="")
    experiment_outcome.add_argument("--severity-weight", type=float, default=1.0)
    experiment_outcome.add_argument("--source", default="operator")
    experiment_outcome.add_argument("--detail")
    experiment_outcome.add_argument("--detail-file")
    experiment_outcome.add_argument("--actor", default="human")
    _set(cmd_review_experiment_outcome, experiment_outcome)

    experiment_report = experiment.add_parser(
        "report",
        help="derive arm metrics and a fail-closed policy candidate",
    )
    experiment_report.add_argument("experiment_id")
    experiment_report.add_argument("--project")
    experiment_report.add_argument("--min-tasks-per-arm", type=int, default=5)
    experiment_report.add_argument(
        "--min-validated-outcomes-per-arm", type=int, default=3
    )
    _set(cmd_review_experiment_report, experiment_report)

    publish = sub.add_parser("publish")
    publish.add_argument("task_id")
    publish.add_argument("target")
    publish.add_argument("created_by")
    publish.add_argument("--evidence-id")
    _set(cmd_publish, publish)

    pr = sub.add_parser(
        "pull-request",
        help="open or inspect pull/merge requests on the task's git host",
    ).add_subparsers(dest="pull_request_command", required=True)
    pr_open = pr.add_parser("open", help="open a PR/MR on github or gitea")
    pr_open.add_argument("--repo-url", required=True, help="https URL of the git repository")
    pr_open.add_argument("--head", required=True, help="branch name with the change")
    pr_open.add_argument("--base", default=None, help="target branch (default: repo default branch)")
    pr_open.add_argument("--title", default=None)
    pr_open.add_argument("--body", default="")
    pr_open.add_argument(
        "--task-id",
        default=None,
        help="if set, also record a pull_request_opened integration finding",
    )
    _set(cmd_pull_request_open, pr_open)

    secret = sub.add_parser("secret", help="secret boundary commands").add_subparsers(dest="secret_command", required=True)
    secret_set = secret.add_parser("set")
    secret_set.add_argument("name")
    secret_set.add_argument("value", nargs="?", default=None, help="secret value (avoid; prefer --from-stdin)")
    secret_set.add_argument("--from-stdin", action="store_true", help="read value from stdin")
    secret_set.add_argument("--from-file", help="read value from file path")
    secret_set.add_argument("--scopes", required=True)
    secret_set.add_argument("--created-by", required=True)
    _set(cmd_secret_set, secret_set)
    secret_list = secret.add_parser("list")
    _set(cmd_secret_list, secret_list)
    secret_delete = secret.add_parser("delete", help="hard-delete a secret (scrub its value)")
    secret_delete.add_argument("secret", help="secret id or name")
    secret_delete.add_argument("--actor", default="operator")
    _set(cmd_secret_delete, secret_delete)
    secret_rotate = secret.add_parser("rotate", help="rotate a secret's value in place (audited)")
    secret_rotate.add_argument("name", help="secret id or name")
    secret_rotate.add_argument("value", nargs="?", default=None, help="new value (avoid; prefer --from-stdin / --from-file)")
    secret_rotate.add_argument("--from-stdin", action="store_true", help="read the new value from stdin")
    secret_rotate.add_argument("--from-file", help="read the new value from a file path")
    secret_rotate.add_argument("--actor", default="operator")
    _set(cmd_secret_rotate, secret_rotate)
    secret_access = secret.add_parser("access")
    secret_access.add_argument("secret")
    secret_access.add_argument("agent_id")
    secret_access.add_argument("--purpose", required=True)
    _set(cmd_secret_access, secret_access)
    audits = secret.add_parser("audits")
    audits.add_argument("--secret-id")
    _set(cmd_secret_audits, audits)

    runtime = sub.add_parser("runtime", help="runtime boundary commands").add_subparsers(dest="runtime_command", required=True)
    runtime_create = runtime.add_parser("create")
    runtime_create.add_argument("name")
    runtime_create.add_argument("--manifest", required=True)
    runtime_create.add_argument("--created-by", required=True)
    _set(cmd_runtime_create, runtime_create)
    runtime_list = runtime.add_parser("list")
    _set(cmd_runtime_list, runtime_list)
    runtime_delta = runtime.add_parser("delta", help="runtime environment delta lifecycle").add_subparsers(dest="runtime_delta_command", required=True)
    runtime_delta_propose = runtime_delta.add_parser("propose")
    runtime_delta_propose.add_argument("task_id")
    runtime_delta_propose.add_argument("agent_id")
    runtime_delta_propose.add_argument("--package-manager", required=True, choices=("pip", "uv", "npm", "pnpm"))
    runtime_delta_propose.add_argument("--commands", required=True, help="JSON list of install commands")
    runtime_delta_propose.add_argument("--dependencies", required=True, help="JSON list of added dependencies")
    runtime_delta_propose.add_argument("--reason", required=True)
    runtime_delta_propose.add_argument("--project")
    runtime_delta_propose.add_argument("--base-runtime", help="base runtime id or name")
    runtime_delta_propose.add_argument("--base-runtime-digest")
    runtime_delta_propose.add_argument("--lockfile-path")
    runtime_delta_propose.add_argument("--lockfile-digest")
    runtime_delta_propose.add_argument("--evidence-id")
    _set(cmd_runtime_delta_propose, runtime_delta_propose)
    runtime_delta_list = runtime_delta.add_parser("list")
    runtime_delta_list.add_argument("--status", choices=("proposed", "validated", "rejected", "promoted"))
    runtime_delta_list.add_argument("--task-id")
    runtime_delta_list.add_argument("--project")
    runtime_delta_list.add_argument("--limit", type=int, default=200)
    _set(cmd_runtime_delta_list, runtime_delta_list)
    runtime_delta_show = runtime_delta.add_parser("show")
    runtime_delta_show.add_argument("delta")
    _set(cmd_runtime_delta_show, runtime_delta_show)
    runtime_delta_validate = runtime_delta.add_parser("validate")
    runtime_delta_validate.add_argument("delta")
    runtime_delta_validate.add_argument("--actor", default="operator")
    _set(cmd_runtime_delta_validate, runtime_delta_validate)
    runtime_delta_reject = runtime_delta.add_parser("reject")
    runtime_delta_reject.add_argument("delta")
    runtime_delta_reject.add_argument("--actor", default="operator")
    runtime_delta_reject.add_argument("--reason", required=True)
    _set(cmd_runtime_delta_reject, runtime_delta_reject)
    runtime_delta_promote = runtime_delta.add_parser("promote")
    runtime_delta_promote.add_argument("delta")
    runtime_delta_promote.add_argument("--actor", default="operator")
    runtime_delta_promote.add_argument("--runtime-name")
    _set(cmd_runtime_delta_promote, runtime_delta_promote)

    artifact = sub.add_parser(
        "artifact",
        help="artifact registry: canonical record for deliverables (images, packages, tarballs)",
    ).add_subparsers(dest="artifact_command", required=True)
    artifact_register = artifact.add_parser("register")
    artifact_register.add_argument("kind", help="e.g. image, package, tarball, wheel")
    artifact_register.add_argument("digest", help="canonical hash, e.g. sha256:abc...")
    artifact_register.add_argument("uri")
    artifact_register.add_argument("--created-by", required=True)
    artifact_register.add_argument("--sbom-uri")
    artifact_register.add_argument("--signers", help="comma-separated signer identities")
    artifact_register.add_argument("--metadata")
    _set(cmd_artifact_register, artifact_register)
    artifact_list = artifact.add_parser("list")
    artifact_list.add_argument("--kind")
    _set(cmd_artifact_list, artifact_list)
    artifact_show = artifact.add_parser("show")
    artifact_show.add_argument("artifact", help="artifact id or digest")
    _set(cmd_artifact_show, artifact_show)
    artifact_delete = artifact.add_parser("delete")
    artifact_delete.add_argument("artifact", help="artifact id or digest")
    artifact_delete.add_argument("--actor", default="operator")
    _set(cmd_artifact_delete, artifact_delete)

    env_root = sub.add_parser(
        "env",
        help="environments and deployments (artifact -> environment edges)",
    ).add_subparsers(dest="env_command", required=True)
    env_register = env_root.add_parser("register")
    env_register.add_argument("name")
    env_register.add_argument("--tenant-id")
    env_register.add_argument("--channel", default="fleet")
    env_register.add_argument("--promotes-from", help="upstream environment id")
    env_register.add_argument("--metadata")
    env_register.add_argument("--created-by", default="human")
    _set(cmd_env_register, env_register)
    env_list = env_root.add_parser("list")
    env_list.add_argument("--tenant-id")
    env_list.add_argument("--channel")
    _set(cmd_env_list, env_list)
    env_show = env_root.add_parser("show")
    env_show.add_argument("environment", help="environment id or name")
    _set(cmd_env_show, env_show)
    env_deploy = env_root.add_parser(
        "deploy",
        help="record a new active deployment in an environment, retiring the prior one",
    )
    env_deploy.add_argument("environment", help="environment id or name")
    env_deploy.add_argument("artifact", help="artifact id or digest")
    env_deploy.add_argument("--actor", required=True)
    env_deploy.add_argument("--metadata")
    _set(cmd_env_deploy, env_deploy)
    env_current = env_root.add_parser("current")
    env_current.add_argument("environment")
    _set(cmd_env_current, env_current)
    env_deployments = env_root.add_parser("history")
    env_deployments.add_argument("environment")
    _set(cmd_env_deployments, env_deployments)

    bridge = sub.add_parser("bridge", help="external project bridge commands").add_subparsers(dest="bridge_command", required=True)
    bridge_import = bridge.add_parser("import")
    bridge_import.add_argument("source")
    bridge_import.add_argument("external_id")
    bridge_import.add_argument("title")
    bridge_import.add_argument("--payload", default="{}")
    bridge_import.add_argument("--description")
    bridge_import.add_argument("--project")
    bridge_import.add_argument("--priority", type=int, default=0)
    bridge_import.add_argument("--required-capabilities")
    bridge_import.add_argument("--dependencies")
    bridge_import.add_argument("--metadata", default="{}")
    bridge_import.add_argument("--actor", default="bridge")
    _set(cmd_bridge_import, bridge_import)
    bridge_list = bridge.add_parser("list")
    _set(cmd_bridge_list, bridge_list)
    bridge_repository = bridge.add_parser("repository", help="registered project repository").add_subparsers(dest="bridge_repository_command", required=True)
    bridge_repository_register = bridge_repository.add_parser("register")
    bridge_repository_register.add_argument("name")
    bridge_repository_register.add_argument("path")
    bridge_repository_register.add_argument("--source")
    bridge_repository_register.add_argument("--project")
    bridge_repository_register.add_argument("--required-capabilities")
    bridge_repository_register.add_argument("--poll-interval-seconds", type=int, default=60)
    bridge_repository_register.add_argument("--metadata", default="{}")
    bridge_repository_register.add_argument("--disabled", action="store_true")
    bridge_repository_register.add_argument("--actor", default="beads-bridge")
    _set(cmd_bridge_repository_register, bridge_repository_register)
    bridge_repository_list = bridge_repository.add_parser("repos")
    bridge_repository_list.add_argument("--enabled", action="store_true", default=None)
    _set(cmd_bridge_repository_list, bridge_repository_list)
    integrations = sub.add_parser("integrations", help="integration authority observations and findings").add_subparsers(dest="integrations_command", required=True)
    integrations_findings = integrations.add_parser("findings")
    integrations_findings.add_argument("--source-kind")
    integrations_findings.add_argument("--source-id")
    integrations_findings.add_argument("--finding-type")
    integrations_findings.add_argument("--status")
    integrations_findings.add_argument("--severity")
    integrations_findings.add_argument("--limit", type=int, default=100)
    _set(cmd_integrations_findings, integrations_findings)
    integrations_observations = integrations.add_parser("observations")
    integrations_observations.add_argument("--source-kind")
    integrations_observations.add_argument("--source-id")
    integrations_observations.add_argument("--authority")
    integrations_observations.add_argument("--status")
    integrations_observations.add_argument("--limit", type=int, default=100)
    _set(cmd_integrations_observations, integrations_observations)

    curiosity = sub.add_parser(
        "curiosity",
        help="read and adjudicate a host's curiosity quarantine THROUGH THE HUB "
        "(the ledger lives in the agent's OpenClaw sandbox; a task sandbox "
        "cannot reach it directly)",
    ).add_subparsers(dest="curiosity_command", required=True)
    curiosity_list = curiosity.add_parser("list", help="list candidates")
    curiosity_list.add_argument(
        "--status", choices=["quarantined", "approved", "rejected"], default=None
    )
    _set(cmd_curiosity_list, curiosity_list)
    for _decision in ("approve", "reject"):
        _parser = curiosity.add_parser(
            _decision, help="%s a quarantined candidate" % _decision
        )
        _parser.add_argument("candidate_id")
        _parser.add_argument("--actor", required=True)
        _parser.add_argument("--reason", required=True)
        _parser.add_argument(
            "--approval-id",
            required=True,
            help="external approval id; use the adjudicating task id so the "
            "promotion is traceable in both the curiosity ledger and task history",
        )
        _parser.set_defaults(decision=_decision)
        _set(cmd_curiosity_decide, _parser)
    memory = sub.add_parser("memory", help="memory and provenance commands").add_subparsers(dest="memory_command", required=True)
    memory_decay = memory.add_parser(
        "decay",
        help="dream-04: forget stale, low-salience memory records (dry-run unless --apply); "
        "curated knowledge (user/project/feedback/deployment_learning/fleet_learning/dream/beads_memory) is preserved",
    )
    memory_decay.add_argument("--ttl-days", type=float, default=90.0)
    memory_decay.add_argument("--limit", type=int, default=500)
    memory_decay.add_argument("--apply", action="store_true", help="actually delete (default: dry-run report)")
    _set(cmd_memory_decay, memory_decay)
    memory_add = memory.add_parser("add")
    memory_add.add_argument("--task-id")
    memory_add.add_argument("--subject-type", required=True)
    memory_add.add_argument("--subject-id")
    memory_add.add_argument("--record-type", required=True)
    memory_add.add_argument("--content", required=True)
    memory_add.add_argument("--evidence-id")
    memory_add.add_argument("--created-by", required=True)
    _set(cmd_memory_add, memory_add)
    memory_search = memory.add_parser("search")
    memory_search.add_argument("--task-id")
    memory_search.add_argument("--subject-type")
    memory_search.add_argument("--subject-id")
    memory_search.add_argument(
        "--record-type",
        help="Exact match on record_type (e.g. nap_summary)",
    )
    memory_search.add_argument(
        "--record-type-prefix",
        help="Prefix match on record_type (e.g. 'dream:' matches dream:reflection, dream:lesson)",
    )
    memory_search.add_argument(
        "--created-by",
        help="Filter by creator (e.g. nap-consolidator, agent_rocky)",
    )
    memory_search.add_argument(
        "--since",
        help="ISO-8601 lower bound on created_at (inclusive)",
    )
    memory_search.add_argument(
        "--until",
        help="ISO-8601 upper bound on created_at (inclusive)",
    )
    memory_search.add_argument(
        "--limit",
        type=int,
        help="Maximum number of records to return",
    )
    memory_search.add_argument(
        "--order",
        choices=["asc", "desc"],
        default="asc",
        help="Sort order: asc (oldest first, default) or desc (newest first)",
    )
    _set(cmd_memory_search, memory_search)

    memory_remember = memory.add_parser(
        "remember",
        help="store an ambient project-scoped fact (bd remember equivalent)",
    )
    memory_remember.add_argument("key")
    memory_remember.add_argument("content")
    memory_remember.add_argument("--project", default="default")
    memory_remember.add_argument("--actor", default="human")
    _set(cmd_memory_remember, memory_remember)

    memory_list = memory.add_parser(
        "list",
        help="list project-scoped memories (bd memories equivalent)",
    )
    memory_list.add_argument("--project", default="default")
    _set(cmd_memory_list, memory_list)

    memory_forget = memory.add_parser(
        "forget",
        help="delete a project-scoped memory by key (bd forget equivalent)",
    )
    memory_forget.add_argument("key")
    memory_forget.add_argument("--project", default="default")
    _set(cmd_memory_forget, memory_forget)

    memory_summarize_actions = memory.add_parser(
        "summarize-actions",
        help="write a bounded memory record from action ledger summaries",
    )
    memory_summarize_actions.add_argument("--agent")
    memory_summarize_actions.add_argument("--since")
    memory_summarize_actions.add_argument("--created-by", default="mac")
    memory_summarize_actions.add_argument(
        "--dry-run",
        action="store_true",
        help="return the summary without writing memory",
    )
    _set(cmd_memory_summarize_actions, memory_summarize_actions)

    # mem-07: embed a memory_record into Qdrant + record the vector_ref.
    memory_embed = memory.add_parser(
        "embed",
        help="embed one memory_record into the vector tier (mem-07)",
    )
    memory_embed.add_argument("memory_id")
    memory_embed.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_embed.add_argument(
        "--qdrant-url",
        help="override the default Qdrant URL (MAC_QDRANT_URL/QDRANT_URL env cascade or 127.0.0.1:6333)",
    )
    _set(cmd_memory_embed, memory_embed)

    memory_backfill = memory.add_parser(
        "backfill",
        help="embed every memory_record not yet in the target Qdrant collection (mem-07)",
    )
    memory_backfill.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_backfill.add_argument(
        "--limit", type=int, help="cap embeddings this pass (None = all)"
    )
    memory_backfill.add_argument("--qdrant-url")
    _set(cmd_memory_backfill, memory_backfill)

    memory_health = memory.add_parser(
        "health",
        help="mem-10: memory-tier health snapshot (counts + alerts for "
        "inert vector tier / stalled consolidator / disk bloat)",
    )
    memory_health.add_argument(
        "--nap-interval-hours", type=float, default=1.0,
        help="2× this value is the stalled-consolidator alert threshold",
    )
    memory_health.add_argument("--qdrant-url")
    _set(cmd_memory_health, memory_health)

    memory_recall = memory.add_parser(
        "recall",
        help="vector-tier recall (mem-09): embed query and return top "
        "ranked memory hits with their summaries",
    )
    memory_recall.add_argument("query")
    memory_recall.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_recall.add_argument("--limit", type=int, default=5)
    memory_recall.add_argument(
        "--min-score", type=float,
        help="drop hits below this cosine score (0.0–1.0)",
    )
    memory_recall.add_argument(
        "--project",
        help="server-side filter: only return hits whose payload project matches",
    )
    memory_recall.add_argument(
        "--tenant-id",
        help="server-side filter: only return hits whose payload tenant matches",
    )
    memory_recall.add_argument(
        "--qdrant-url",
        help="override Qdrant URL when running in local mode (--db). "
        "Hub mode reads the Qdrant env cascade on the hub.",
    )
    _set(cmd_memory_recall, memory_recall)

    memory_recall_dreams = memory.add_parser(
        "recall-dreams",
        help="recall typed dream artifacts with scope/kind/confidence filters",
    )
    memory_recall_dreams.add_argument("query")
    memory_recall_dreams.add_argument(
        "--tier", choices=("medium", "long"), default="medium"
    )
    memory_recall_dreams.add_argument("--limit", type=int, default=5)
    memory_recall_dreams.add_argument(
        "--min-score",
        type=float,
        help="drop vector hits below this cosine score (0.0-1.0)",
    )
    memory_recall_dreams.add_argument("--project")
    memory_recall_dreams.add_argument("--agent-id")
    memory_recall_dreams.add_argument(
        "--scope", choices=("agent", "project", "fleet")
    )
    memory_recall_dreams.add_argument(
        "--kind",
        choices=(
            "decision_rule",
            "failure_pattern",
            "knowledge_snippet",
            "tool_pattern",
            "routing_signal",
        ),
    )
    memory_recall_dreams.add_argument(
        "--min-confidence", choices=("low", "medium", "high")
    )
    memory_recall_dreams.add_argument("--tenant-id")
    memory_recall_dreams.add_argument(
        "--qdrant-url",
        help="override Qdrant URL when running in local mode (--db). "
        "Hub mode reads the Qdrant env cascade on the hub.",
    )
    _set(cmd_memory_recall_dreams, memory_recall_dreams)

    rollout = sub.add_parser("rollout", help="rollout and rescue commands").add_subparsers(dest="rollout_command", required=True)
    rollout_create = rollout.add_parser("create")
    rollout_create.add_argument("version")
    rollout_create.add_argument("strategy")
    rollout_create.add_argument("--target-percent", type=int, default=10)
    rollout_create.add_argument("--created-by", required=True)
    rollout_create.add_argument("--tenant-id")
    rollout_create.add_argument("--channel", default="fleet")
    rollout_create.add_argument("--runtime")
    rollout_create.add_argument("--artifact-uri")
    rollout_create.add_argument("--artifact-hash")
    rollout_create.add_argument("--health-policy")
    rollout_create.add_argument("--required-eval-set-id")
    _set(cmd_rollout_create, rollout_create)
    rollout_list = rollout.add_parser("list")
    rollout_list.add_argument("--tenant-id")
    rollout_list.add_argument("--channel")
    _set(cmd_rollout_list, rollout_list)
    rollout_advance = rollout.add_parser("advance")
    rollout_advance.add_argument("rollout_id")
    rollout_advance.add_argument("action")
    rollout_advance.add_argument("--actor", required=True)
    rollout_advance.add_argument("--detail")
    _set(cmd_rollout_advance, rollout_advance)
    rollout_artifact = rollout.add_parser("verify-artifact")
    rollout_artifact.add_argument("rollout_id")
    rollout_artifact.add_argument("--artifact-uri", required=True)
    rollout_artifact.add_argument("--artifact-hash", required=True)
    rollout_artifact.add_argument("--actor", required=True)
    _set(cmd_rollout_verify_artifact, rollout_artifact)
    rollout_health = rollout.add_parser("health")
    rollout_health.add_argument("rollout_id")
    rollout_health.add_argument("--checks", required=True)
    rollout_health.add_argument("--actor", required=True)
    _set(cmd_rollout_health, rollout_health)
    rollout_rescue = rollout.add_parser("rescue")
    rollout_rescue.add_argument("rollout_id")
    rollout_rescue.add_argument("--actor", required=True)
    rollout_rescue.add_argument("--reason", required=True)
    rollout_rescue.add_argument("--detail")
    _set(cmd_rollout_rescue, rollout_rescue)

    events = sub.add_parser("events", help="unified audit stream").add_subparsers(
        dest="events_command", required=True
    )
    events_list = events.add_parser(
        "list",
        help="list events across task/rollout/eval_set/secret audit surfaces",
    )
    events_list.add_argument(
        "--subject-type",
        choices=(
            "task",
            "agent",
            "project",
            "fleet",
            "rollout",
            "eval_set",
            "secret",
            "environment",
            "conversation_thread",
            "vector_ref",
        ),
    )
    events_list.add_argument("--subject-id")
    events_list.add_argument("--actor")
    events_list.add_argument("--event-type", help="exact event_type match")
    events_list.add_argument(
        "--prefix",
        help="event_type prefix (e.g. 'rollout.' for all rollout events)",
    )
    events_list.add_argument("--since", help="ISO timestamp lower bound (inclusive)")
    events_list.add_argument("--until", help="ISO timestamp upper bound (inclusive)")
    events_list.add_argument("--limit", type=int, default=100)
    _set(cmd_events_list, events_list)

    action_events = sub.add_parser(
        "action-events",
        help="canonical MAC action event ledger",
    ).add_subparsers(dest="action_events_command", required=True)
    action_events_list = action_events.add_parser("list")
    action_events_list.add_argument("--agent-id")
    action_events_list.add_argument("--task-id")
    action_events_list.add_argument("--session-id")
    action_events_list.add_argument("--sandbox-id")
    action_events_list.add_argument("--policy-id")
    action_events_list.add_argument("--action-type")
    action_events_list.add_argument(
        "--outcome",
        choices=("unknown", "started", "success", "failure", "denied", "allowed", "skipped"),
    )
    action_events_list.add_argument("--since")
    action_events_list.add_argument("--until")
    action_events_list.add_argument("--limit", type=int, default=100)
    _set(cmd_action_events_list, action_events_list)

    action_events_stream = action_events.add_parser("stream")
    action_events_stream.add_argument("--agent-id")
    action_events_stream.add_argument("--task-id")
    action_events_stream.add_argument("--session-id")
    action_events_stream.add_argument("--sandbox-id")
    action_events_stream.add_argument("--policy-id")
    action_events_stream.add_argument("--action-type")
    action_events_stream.add_argument(
        "--outcome",
        choices=("unknown", "started", "success", "failure", "denied", "allowed", "skipped"),
    )
    action_events_stream.add_argument("--since")
    action_events_stream.add_argument("--limit", type=int, default=100)
    action_events_stream.add_argument("--timeout", type=float, default=0.0)
    action_events_stream.add_argument("--interval", type=float, default=1.0)
    action_events_stream.add_argument("--follow", action="store_true")
    _set(cmd_action_events_stream, action_events_stream)

    action_events_export = action_events.add_parser("export-otlp")
    action_events_export.add_argument("--agent-id")
    action_events_export.add_argument("--task-id")
    action_events_export.add_argument("--session-id")
    action_events_export.add_argument("--sandbox-id")
    action_events_export.add_argument("--policy-id")
    action_events_export.add_argument("--action-type")
    action_events_export.add_argument(
        "--outcome",
        choices=("unknown", "started", "success", "failure", "denied", "allowed", "skipped"),
    )
    action_events_export.add_argument("--since")
    action_events_export.add_argument("--until")
    action_events_export.add_argument("--limit", type=int, default=1000)
    _set(cmd_action_events_export_otlp, action_events_export)

    command_audit = sub.add_parser(
        "command-audit", help="short-retention per-agent command log"
    ).add_subparsers(dest="command_audit_command", required=True)
    command_audit_list = command_audit.add_parser(
        "list", help="list audited command start/completion events"
    )
    command_audit_list.add_argument("--agent-id")
    command_audit_list.add_argument("--task-id")
    command_audit_list.add_argument("--command-id")
    command_audit_list.add_argument(
        "--phase", choices=("started", "completed", "failed", "timeout", "error")
    )
    command_audit_list.add_argument("--since", help="ISO timestamp lower bound")
    command_audit_list.add_argument("--until", help="ISO timestamp upper bound")
    command_audit_list.add_argument("--limit", type=int, default=100)
    _set(cmd_command_audit_list, command_audit_list)

    observability = sub.add_parser(
        "observability", help="structured metric/log observations"
    ).add_subparsers(dest="observability_command", required=True)
    observability_list = observability.add_parser(
        "list", help="list structured observability metrics and logs"
    )
    observability_list.add_argument("--kind", choices=("metric", "log"))
    observability_list.add_argument("--layer")
    observability_list.add_argument("--level")
    observability_list.add_argument("--name")
    observability_list.add_argument("--subject-type")
    observability_list.add_argument("--subject-id")
    observability_list.add_argument("--since", help="ISO timestamp lower bound")
    observability_list.add_argument("--until", help="ISO timestamp upper bound")
    observability_list.add_argument("--after-sequence", type=int)
    observability_list.add_argument("--limit", type=int, default=100)
    _set(cmd_observability_list, observability_list)
    observability_prune = observability.add_parser(
        "prune",
        help="delete observability_events older than --older-than (ISO timestamp) "
        "or keep only --keep-last rows; returns the number removed",
    )
    observability_prune.add_argument(
        "--older-than",
        help="ISO timestamp; rows with created_at < this are deleted",
    )
    observability_prune.add_argument(
        "--keep-last",
        type=int,
        help="keep only the most recent N rows by sequence",
    )
    _set(cmd_observability_prune, observability_prune)

    communication = sub.add_parser(
        "communication",
        aliases=["comm"],
        help="logical public identities, representation, and OpenClaw delivery",
    ).add_subparsers(dest="communication_command", required=True)

    communication_identity = communication.add_parser(
        "identity", help="manage stable human-facing identities"
    ).add_subparsers(dest="communication_identity_command", required=True)
    communication_identity_configure = communication_identity.add_parser("configure")
    communication_identity_configure.add_argument("name")
    communication_identity_configure.add_argument("--display-name")
    communication_identity_configure.add_argument("--description")
    communication_identity_configure.add_argument("--default", action="store_true")
    communication_identity_configure.add_argument("--disabled", action="store_true")
    communication_identity_configure.add_argument("--metadata", default="{}")
    _set(cmd_communication_identity_configure, communication_identity_configure)
    communication_identity_list = communication_identity.add_parser("list")
    communication_identity_list.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction
    )
    _set(cmd_communication_identity_list, communication_identity_list)
    communication_identity_show = communication_identity.add_parser("show")
    communication_identity_show.add_argument("identity")
    _set(cmd_communication_identity_show, communication_identity_show)
    communication_identity_delete = communication_identity.add_parser("delete")
    communication_identity_delete.add_argument("identity")
    _set(cmd_communication_identity_delete, communication_identity_delete)

    communication_account = communication.add_parser(
        "account", help="manage channel accounts owned by identities"
    ).add_subparsers(dest="communication_account_command", required=True)
    communication_account_configure = communication_account.add_parser("configure")
    communication_account_configure.add_argument("identity")
    communication_account_configure.add_argument("channel")
    communication_account_configure.add_argument("--account-id", default="default")
    communication_account_configure.add_argument("--credential-refs", default="{}")
    communication_account_configure.add_argument("--config", default="{}")
    communication_account_configure.add_argument("--disabled", action="store_true")
    _set(cmd_communication_account_configure, communication_account_configure)
    communication_account_list = communication_account.add_parser("list")
    communication_account_list.add_argument("--identity")
    communication_account_list.add_argument("--channel")
    communication_account_list.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction
    )
    _set(cmd_communication_account_list, communication_account_list)
    communication_account_show = communication_account.add_parser("show")
    communication_account_show.add_argument("account")
    _set(cmd_communication_account_show, communication_account_show)
    communication_account_delete = communication_account.add_parser("delete")
    communication_account_delete.add_argument("account")
    _set(cmd_communication_account_delete, communication_account_delete)

    communication_representation = communication.add_parser(
        "representation", help="map internal agents/roles/projects to public identities"
    ).add_subparsers(dest="communication_representation_command", required=True)
    communication_representation_configure = communication_representation.add_parser(
        "configure"
    )
    communication_representation_configure.add_argument(
        "subject_kind", choices=("agent", "role", "project", "fleet")
    )
    communication_representation_configure.add_argument("subject_id")
    communication_representation_configure.add_argument("--identity")
    communication_representation_configure.add_argument(
        "--mode", choices=("direct", "delegated", "internal_only"), default="delegated"
    )
    communication_representation_configure.add_argument("--priority", type=int, default=100)
    communication_representation_configure.add_argument("--disabled", action="store_true")
    communication_representation_configure.add_argument("--metadata", default="{}")
    _set(
        cmd_communication_representation_configure,
        communication_representation_configure,
    )
    communication_representation_list = communication_representation.add_parser("list")
    communication_representation_list.add_argument("--subject-kind")
    communication_representation_list.add_argument("--identity")
    communication_representation_list.add_argument(
        "--enabled", action=argparse.BooleanOptionalAction
    )
    _set(cmd_communication_representation_list, communication_representation_list)
    communication_representation_resolve = communication_representation.add_parser("resolve")
    communication_representation_resolve.add_argument("agent_id")
    communication_representation_resolve.add_argument("--project")
    communication_representation_resolve.add_argument("--role")
    communication_representation_resolve.add_argument(
        "--fleet", dest="representation_fleet", default="default"
    )
    _set(cmd_communication_representation_resolve, communication_representation_resolve)
    communication_representation_delete = communication_representation.add_parser("delete")
    communication_representation_delete.add_argument("binding_id")
    _set(cmd_communication_representation_delete, communication_representation_delete)

    communication_lease = communication.add_parser(
        "lease", help="manage singleton gateway ownership of channel accounts"
    ).add_subparsers(dest="communication_lease_command", required=True)
    communication_lease_acquire = communication_lease.add_parser("acquire")
    communication_lease_acquire.add_argument("account_id")
    communication_lease_acquire.add_argument("agent_id")
    communication_lease_acquire.add_argument("--lease-seconds", type=int, default=90)
    communication_lease_acquire.add_argument("--metadata", default="{}")
    _set(cmd_communication_lease_acquire, communication_lease_acquire)
    communication_lease_list = communication_lease.add_parser("list")
    communication_lease_list.add_argument("--agent-id")
    communication_lease_list.add_argument("--active-only", action="store_true")
    _set(cmd_communication_lease_list, communication_lease_list)
    communication_lease_renew = communication_lease.add_parser("renew")
    communication_lease_renew.add_argument("lease_id")
    communication_lease_renew.add_argument("agent_id")
    communication_lease_renew.add_argument("fencing_token")
    communication_lease_renew.add_argument("--lease-seconds", type=int, default=90)
    _set(cmd_communication_lease_renew, communication_lease_renew)
    communication_lease_release = communication_lease.add_parser("release")
    communication_lease_release.add_argument("lease_id")
    communication_lease_release.add_argument("agent_id")
    communication_lease_release.add_argument("fencing_token")
    _set(cmd_communication_lease_release, communication_lease_release)

    communication_send = communication.add_parser(
        "send", help="enqueue an idempotent OpenClaw human-facing delivery"
    )
    communication_send.add_argument("target")
    communication_send.add_argument("body", nargs="?", default="")
    communication_send.add_argument("--body-file")
    communication_send.add_argument("--origin-agent-id")
    communication_send.add_argument("--identity")
    communication_send.add_argument("--account-id")
    communication_send.add_argument("--channel")
    communication_send.add_argument("--task-id")
    communication_send.add_argument("--idempotency-key")
    communication_send.add_argument("--max-attempts", type=int, default=5)
    communication_send.add_argument("--metadata", default="{}")
    _set(cmd_communication_send, communication_send)
    communication_deliveries = communication.add_parser("deliveries")
    communication_deliveries.add_argument("--status")
    communication_deliveries.add_argument("--identity")
    communication_deliveries.add_argument("--origin-agent-id")
    communication_deliveries.add_argument("--limit", type=int, default=100)
    _set(cmd_communication_deliveries, communication_deliveries)

    notifier = sub.add_parser(
        "notifier", help="operator notification channel configuration"
    ).add_subparsers(dest="notifier_command", required=True)
    notifier_configure = notifier.add_parser("configure")
    notifier_configure.add_argument("name")
    notifier_configure.add_argument("channel_type", choices=("hermes", "slack", "telegram"))
    notifier_configure.add_argument("--event-types", default="task.*")
    notifier_configure.add_argument("--target", default="{}")
    notifier_configure.add_argument("--metadata", default="{}")
    notifier_configure.add_argument("--disabled", action="store_true")
    _set(cmd_notifier_configure, notifier_configure)
    notifier_list = notifier.add_parser("list")
    notifier_list.add_argument("--enabled", action=argparse.BooleanOptionalAction)
    notifier_list.add_argument("--channel-type", choices=("hermes", "slack", "telegram"))
    _set(cmd_notifier_list, notifier_list)
    notifier_delete = notifier.add_parser("delete")
    notifier_delete.add_argument("channel_id_or_name")
    _set(cmd_notifier_delete, notifier_delete)
    notifier_deliver = notifier.add_parser("deliver")
    notifier_deliver.add_argument("--limit", type=int, default=50)
    notifier_deliver.add_argument("--notification-id")
    _set(cmd_notifier_deliver, notifier_deliver)

    migrate = sub.add_parser(
        "migrate",
        help="one-time migration from external systems",
    ).add_subparsers(dest="migrate_command", required=True)
    migrate_import = migrate.add_parser(
        "import",
        help="replay a JSONL stream of {record: tenant|user|task|evidence|history} rows",
    )
    migrate_import.add_argument("path", help="path to JSONL file")
    _set(cmd_migrate_import, migrate_import)
    migrate_acc = migrate.add_parser(
        "acc",
        help="dry-run or import an ACC SQLite database once",
    )
    migrate_acc.add_argument("acc_db", help="path to ACC SQLite DB, e.g. ~/.acc/data/acc.db")
    migrate_acc.add_argument("--mode", choices=("dry-run", "import"), default="dry-run")
    migrate_acc.add_argument(
        "--allow-active",
        action="store_true",
        help="import claimed/in-progress ACC tasks as requeued mac tasks",
    )
    migrate_acc.add_argument(
        "--audit-limit",
        type=int,
        default=1000,
        help="latest ACC work_audit_events rows to carry as task provenance; 0 skips audit rows",
    )
    migrate_acc.add_argument(
        "--agent-home",
        help="home directory used for soul snapshot path hints; defaults to current home",
    )
    migrate_acc.add_argument("--report", help="write the migration report JSON to this path")
    _set(cmd_migrate_acc, migrate_acc)

    workflow = sub.add_parser(
        "workflow",
        help="workflow inspection (graph definitions, runs, decision gates)",
    ).add_subparsers(dest="workflow_command", required=True)
    workflow_decisions = workflow.add_parser(
        "decisions",
        help="list every human-decision (approval) gate in a workflow or a "
        "live run. Pass a workflow id/slug to see the definition's gates, "
        "or a run id (prefix run_) for live state.",
    )
    workflow_decisions.add_argument(
        "id_or_slug",
        help="workflow id/slug, or workflow-run id (prefix `run_`)",
    )
    workflow_decisions.add_argument(
        "--tenant-id",
        help="scope a slug lookup to a tenant",
    )
    _set(cmd_workflow_decisions, workflow_decisions)

    workflow_start = workflow.add_parser(
        "start",
        help="start a workflow run, optionally with front-loaded approval "
        "decisions so the run can advance unattended.",
    )
    workflow_start.add_argument("workflow_id_or_slug")
    workflow_start.add_argument("--started-by", default="human")
    workflow_start.add_argument("--tenant-id")
    workflow_start.add_argument(
        "--input",
        default=None,
        help="JSON object passed to the workflow as initial input",
    )
    workflow_start.add_argument(
        "--pre-decision",
        action="append",
        default=[],
        metavar="NODE_KEY=approved|rejected",
        help="pre-supplied decision for an approval node; may repeat "
        "(e.g. --pre-decision pm_review=approved --pre-decision qa=rejected)",
    )
    _set(cmd_workflow_start, workflow_start)

    eval_root = sub.add_parser("eval", help="evaluation sets and runs").add_subparsers(
        dest="eval_command", required=True
    )
    eval_set_grp = eval_root.add_parser("set", help="eval set commands").add_subparsers(
        dest="eval_set_command", required=True
    )
    eval_set_create = eval_set_grp.add_parser("create")
    eval_set_create.add_argument("name")
    eval_set_create.add_argument(
        "--scoring", choices=("higher_is_better", "lower_is_better"), default="higher_is_better"
    )
    eval_set_create.add_argument("--description", default="")
    eval_set_create.add_argument("--baseline-score", type=float, default=None)
    eval_set_create.add_argument("--regression-threshold", type=float, default=0.0)
    eval_set_create.add_argument("--metadata")
    eval_set_create.add_argument("--created-by", default="human")
    _set(cmd_eval_set_create, eval_set_create)
    eval_set_list = eval_set_grp.add_parser("list")
    _set(cmd_eval_set_list, eval_set_list)
    eval_set_show = eval_set_grp.add_parser("show")
    eval_set_show.add_argument("eval_set")
    _set(cmd_eval_set_show, eval_set_show)
    eval_set_baseline = eval_set_grp.add_parser("baseline")
    eval_set_baseline.add_argument("eval_set")
    eval_set_baseline.add_argument("baseline_score", type=float)
    eval_set_baseline.add_argument("--actor", default="human")
    _set(cmd_eval_set_baseline, eval_set_baseline)

    eval_run_grp = eval_root.add_parser("run", help="eval run commands").add_subparsers(
        dest="eval_run_command", required=True
    )
    eval_run_record = eval_run_grp.add_parser("record")
    eval_run_record.add_argument("eval_set")
    eval_run_record.add_argument(
        "target_kind",
        choices=("rollout_version", "runtime_environment", "agent_build"),
    )
    eval_run_record.add_argument("target_id")
    eval_run_record.add_argument("score", type=float)
    eval_run_record.add_argument("--detail")
    eval_run_record.add_argument("--evidence-id")
    eval_run_record.add_argument("--created-by", default="human")
    _set(cmd_eval_run_record, eval_run_record)
    eval_run_list = eval_run_grp.add_parser("list")
    eval_run_list.add_argument("--eval-set", dest="eval_set")
    eval_run_list.add_argument("--target-id")
    _set(cmd_eval_run_list, eval_run_list)

    plan = sub.add_parser(
        "plan",
        help="planning helpers (topology ordering, blast radius, etc.)",
    ).add_subparsers(dest="plan_command", required=True)
    plan_order = plan.add_parser(
        "order",
        help="order files/modules by import/call topology (leaf-first or core-first layers)",
    )
    plan_order.add_argument(
        "paths",
        nargs="+",
        help="file or module paths to order (relative to --repo)",
    )
    plan_order.add_argument(
        "--repo",
        default=".",
        help="repository root where .codegraph/ lives (default: .)",
    )
    plan_order.add_argument(
        "--core-first",
        action="store_true",
        dest="core_first",
        help="reverse the default leaf-first ordering so core (highest fan-in) comes first",
    )
    _set(cmd_plan_order, plan_order)

    # Discoverability layer, applied over the fully-built tree rather than
    # at 265 registration sites: `help` as a verb at every level, the full
    # CRUD vocabulary on the four first-class objects, and CRUD-first
    # grouped help. Additive only -- every existing command keeps working.
    from mac.cli_surface import install as _install_cli_surface

    return _install_cli_surface(parser)


def _hub_error_message(exc: BaseException) -> Optional[str]:
    """A one-line, actionable rendering of a hub refusal, or None to re-raise.

    Returns None for anything that is not a hub transport error, so genuine
    bugs keep their traceback -- suppressing those would trade one bad
    experience for a worse one.
    """
    if type(exc).__name__ != "HubClientError":
        return None
    raw = str(exc)
    detail = raw
    # "HTTP 400 Bad Request: {"detail":"cannot transition ..."}"
    _, _, body = raw.partition(": ")
    if body.strip().startswith("{"):
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict) and parsed.get("detail"):
                detail = str(parsed["detail"])
        except ValueError:
            pass
    hint = _transition_hint(detail)
    return detail + hint


def _transition_hint(detail: str) -> str:
    """Say what IS possible when the hub refuses a state change.

    "cannot transition task from failed to cancelled" is true and complete and
    still leaves the reader guessing. The legal moves are in TASK_TRANSITIONS,
    so name them rather than making someone read the state machine.
    """
    match = re.search(
        r"cannot transition task from (\w+) to (\w+)", detail or ""
    )
    if not match:
        return ""
    current = match.group(1)
    try:
        from mac.models import TASK_TRANSITIONS

        allowed = sorted(TASK_TRANSITIONS.get(current, set()))
    except Exception:  # noqa: BLE001 - hint only
        return ""
    if not allowed:
        return "\n  %s is terminal; nothing can move it." % current
    verbs = {"open": "`mac task reopen <id>`"}
    routes = ", ".join("%s (%s)" % (state, verbs.get(state, state)) if state in verbs else state
                       for state in allowed)
    extra = ""
    if current == "failed" and "open" in allowed:
        extra = (
            "\n  To discard it instead: reopen first, then `mac task cancel` -- "
            "cancelling is only legal from a live state."
        )
    return "\n  From %s the only legal move is: %s%s" % (current, routes, extra)


def _redirect_moved_command(raw: Sequence[str]) -> None:
    """Point an old top-level spelling at its new home under `mac admin`.

    The administrative commands moved so that `mac` surfaces the four objects
    it models. argparse would answer "invalid choice: 'fleet'", which reads as
    "that command is gone" -- and the first thing anyone does with that is go
    looking for what replaced it. Say so instead.
    """
    from mac.cli_surface import first_positional, moved_to_admin

    # first_positional skips options AND their values. Scanning for the first
    # token that does not start with "-" would land on the value of --db, and
    # this redirect would silently never fire for anyone passing one -- the
    # same mistake the leaf help interception made.
    token = first_positional(build_parser(), raw)
    if token is not None and moved_to_admin(token):
        sys.stderr.write(
            "mac: `%s` moved under `admin`. Run `mac admin %s` "
            "(or `mac admin help` to see everything there).\n" % (token, token)
        )
        raise SystemExit(2)


def main(argv: Optional[Iterable[str]] = None) -> int:
    raw = list(argv) if argv is not None else list(sys.argv[1:])
    # --json is position-independent: strip it before argparse (so it works after
    # the subcommand too) and switch output mode. Text is the default.
    if "--json" in raw:
        _set_output_json(True)
        raw = [a for a in raw if a != "--json"]
    parser = build_parser()
    # `help` after a LEAF command is a help request, not data. Without this,
    # `mac task create help` files a task titled "help" -- a beginner exploring
    # the CLI writes junk into the ledger and learns nothing.
    from mac.cli_surface import leaf_help_request

    leaf = leaf_help_request(parser, raw)
    if leaf is not None:
        leaf.print_help()
        print(
            "\nTo pass 'help' as a value rather than ask for help, use: -- help",
        )
        return 0
    _redirect_moved_command(raw)
    args = parser.parse_args(raw)
    try:
        args.func(args)
    except MACError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print("invalid JSON: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 - a hub refusal is not a crash
        # HubClientError is a RuntimeError, not a MACError, so a refusal the
        # hub stated perfectly clearly -- "cannot transition task from failed
        # to cancelled" -- reached the user as a 30-line traceback through
        # urllib. A stack trace is what you print when you do not know what
        # happened; the hub told us exactly what happened.
        message = _hub_error_message(exc)
        if message is None:
            raise
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
