"""Release identity: what each worker is actually running, against what was selected.

A worker's Git checkout is not its running version: a checkout advanced without
a restart, a wrapper edited in place, or a process that never restarted all
looked "healthy on <sha>". Workers now report the commit their *process* loaded
(``loaded_commit``, captured at boot with a ``boot_id``) alongside the checkout
on disk, and ``restart_pending`` when the two differ.

The operator selects one release (``release_selection``). While one is
selected, every agent that reports a MAC source state must be running exactly
that commit, from a clean checkout, with no restart pending, or it is excluded
from dispatch. Missing or stale evidence is ``unknown``, never a match. Agents
that report no source state (interactive sessions, the hub's in-process
reviewer and operator) are not MAC runtime executors and are not gated.

Readiness is reported as separate facts, never folded into one "healthy":
liveness, release match, dispatch hold, and fleet-context freshness.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional

SOURCE_SCHEMA = "mac.worker_source_state.v1"
DRIFT_SCHEMA = "mac.release_drift.v1"

#: Reasons an agent is excluded from dispatch under a selected release.
REASONS = ("release_unknown", "release_mismatch", "release_restart_pending", "release_dirty")


def _parse(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def source_state(resources: Any) -> Optional[Dict[str, Any]]:
    """The agent's reported MAC source state, or None if it reports none."""
    if not isinstance(resources, Mapping):
        return None
    state = resources.get("source_state")
    if not isinstance(state, Mapping) or state.get("schema") != SOURCE_SCHEMA:
        return None
    return dict(state)


def evaluate(resources: Any, selected_commit: str) -> Optional[str]:
    """None when the agent may run work under ``selected_commit``, else a reason."""
    state = source_state(resources)
    if state is None:
        return None
    loaded = str(state.get("loaded_commit") or "")
    if not loaded or state.get("error"):
        return "release_unknown"
    disk = str(state.get("commit_sha") or "")
    if loaded != selected_commit:
        # The selected commit is on disk but not loaded: a restart fixes it.
        return "release_restart_pending" if disk == selected_commit else "release_mismatch"
    if state.get("restart_pending") is True or disk != loaded:
        return "release_restart_pending"
    if state.get("dirty") is not False:
        return "release_dirty"
    return None


def drift(
    selection: Optional[Mapping[str, Any]],
    agents: List[Mapping[str, Any]],
    *,
    now: Optional[datetime] = None,
    fleet_context_age: Optional[Mapping[str, Optional[float]]] = None,
) -> Dict[str, Any]:
    """Desired versus observed release for every agent that reports a source state.

    ``agents`` are agent dicts (``id``, ``name``, ``status``, ``resources``,
    ``dispatch_hold``, ``last_seen_at``). Each row reports liveness, the loaded
    and on-disk commits, the release verdict, the hold, and — when given —
    the age of the agent's fleet-context refresh, each as its own field.
    """
    now = now or datetime.now(timezone.utc)
    commit = str((selection or {}).get("commit_sha") or "")
    converge_by = _parse((selection or {}).get("converge_by"))
    rows: List[Dict[str, Any]] = []
    for agent in agents:
        state = source_state(agent.get("resources"))
        if state is None:
            continue
        verdict = evaluate(agent.get("resources"), commit) if commit else None
        row: Dict[str, Any] = {
            "agent_id": agent.get("id"),
            "name": agent.get("name"),
            "live": str(agent.get("status") or "") != "offline",
            "last_seen_at": agent.get("last_seen_at"),
            "loaded_commit": state.get("loaded_commit") or "",
            "disk_commit": state.get("commit_sha") or "",
            "boot_id": state.get("boot_id") or "",
            "restart_pending": state.get("restart_pending") is True,
            "dirty": state.get("dirty") is not False,
            "release": "unselected" if not commit else (verdict or "match"),
            "dispatch_hold": bool(agent.get("dispatch_hold")),
        }
        if fleet_context_age is not None:
            row["fleet_context_age_seconds"] = fleet_context_age.get(str(agent.get("id")))
        if commit and verdict:
            row["repair"] = _repair(row, commit)
        rows.append(row)
    drifted = [row for row in rows if row["release"] not in ("match", "unselected")]
    overdue = bool(commit and converge_by and now > converge_by and drifted)
    return {
        "schema": DRIFT_SCHEMA,
        "selection": dict(selection) if selection else None,
        "agents": rows,
        "drifted": [row["name"] for row in drifted],
        "converged": bool(commit) and not drifted,
        "overdue": overdue,
    }


def _repair(row: Mapping[str, Any], commit: str) -> str:
    name = row.get("name") or row.get("agent_id")
    if not row["live"]:
        return "%s is offline; it stays excluded until it runs %s" % (name, commit[:12])
    if row["release"] == "release_restart_pending":
        return "%s has %s on disk but not loaded: restart it (fleet-update %s %s)" % (
            name,
            str(row["disk_commit"])[:12],
            name,
            commit[:12],
        )
    return "scripts/fleet-update %s %s" % (name, commit[:12])
