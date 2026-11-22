"""agent_liveness - heartbeat publisher and silent-agent watchdog.

Provides two capabilities:

1. **Heartbeat** (``publish_heartbeat``): publish a typed
   ``agent.heartbeat.v1`` event to the MAC AgentBus broadcast feed.
   Call from any cron job to prove the agent is alive.

2. **Watchdog** (``check_silent_agents``): scan the broadcast feed for all
   registered agents and return any that have not been heard from within
   ``threshold_hours`` (default 4).  Optionally publish a
   ``agent.silent.alert.v1`` alert back to the bus for each silent agent
   so the hub operator can see the gap.

Usage::

    # Publish a heartbeat (call from each agent's becoming cron):
    python -m mac.agent_liveness heartbeat --agent agent_bullwinkle

    # Check for silent agents (call from watchdog cron):
    python -m mac.agent_liveness watchdog --threshold-hours 4

Environment (all optional -- falls back to ~/.mac/mac.env):

    MAC_HUB_URL          -- hub API base URL (default http://100.72.16.110:8789)
    MAC_WORKER_TOKEN     -- bearer token for hub auth
    FLEET_LIVENESS_AGENT -- agent_id used for watchdog queries (overrides MAC_AGENT_ID; default: agent_bullwinkle)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Schema constants
# ---------------------------------------------------------------------------

HEARTBEAT_SCHEMA = "mac.agentbus.agent_heartbeat.v1"
HEARTBEAT_EVENT_TYPE = "agent.heartbeat.v1"

SILENT_ALERT_SCHEMA = "mac.agentbus.agent_silent_alert.v1"
SILENT_ALERT_EVENT_TYPE = "agent.silent.alert.v1"

# ---------------------------------------------------------------------------
# Config helpers (mirrors fleet_brain_bus_sync pattern)
# ---------------------------------------------------------------------------

from mac import mac_paths as _mac_paths


def _env_file_candidates():
    """Return env file candidates via mac_paths (no hard-coded home literals)."""
    return [_mac_paths.mac_env_file(), _mac_paths.deploy_env_file()]


_DEFAULT_HUB = "http://100.72.16.110:8789"
_DEFAULT_AGENT = "agent_bullwinkle"
_DEFAULT_THRESHOLD_HOURS = 4


def _load_mac_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for path in _env_file_candidates():
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    env.update(
        {
            k: v
            for k, v in os.environ.items()
            if k in env or k.startswith("MAC_") or k.startswith("FLEET_")
        }
    )
    return env


def _get_config() -> dict[str, str]:
    env = _load_mac_env()
    return {
        "hub": env.get("MAC_HUB_URL", _DEFAULT_HUB).rstrip("/"),
        "token": env.get("MAC_WORKER_TOKEN", env.get("MAC_API_TOKEN", "")),
        "agent": env.get("FLEET_LIVENESS_AGENT", env.get("MAC_AGENT_ID", _DEFAULT_AGENT)),
    }


def _bearer(token: str) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def _post_json(url: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", **_bearer(token)},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode(errors="replace")
        raise RuntimeError(f"POST {url} -> HTTP {exc.code}: {body}") from exc


def _get_json(url: str, token: str) -> Any:
    req = urllib.request.Request(url, headers=_bearer(token))
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode(errors="replace")
        raise RuntimeError(f"GET {url} -> HTTP {exc.code}: {body}") from exc


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def publish_heartbeat(
    agent_id: str,
    *,
    hub: str = _DEFAULT_HUB,
    token: str = "",
    note: str = "",
) -> dict[str, Any]:
    """Publish a heartbeat broadcast event for *agent_id*.

    Returns the hub response dict.  Raises ``RuntimeError`` on HTTP failure.
    """
    hub = hub.rstrip("/")
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "schema": HEARTBEAT_SCHEMA,
        "agent_id": agent_id,
        "timestamp": now_iso,
    }
    if note:
        payload["note"] = note

    body = {
        "agent_id": agent_id,
        "event_type": HEARTBEAT_EVENT_TYPE,
        "payload": payload,
    }
    return _post_json(f"{hub}/agentbus/broadcast", body, token)


# ---------------------------------------------------------------------------
# Watchdog helpers
# ---------------------------------------------------------------------------


def _list_registered_agents(hub: str, token: str) -> list[str]:
    """Return all registered agent IDs from the hub."""
    agents = _get_json(f"{hub}/agents", token)
    if isinstance(agents, list):
        return [a["id"] if isinstance(a, dict) else str(a) for a in agents]
    # fallback: hub may return dict
    if isinstance(agents, dict) and "agents" in agents:
        return [a["id"] if isinstance(a, dict) else str(a) for a in agents["agents"]]
    return []


def _latest_broadcast_timestamp(
    hub: str, token: str, agent_id: str, event_type: str = HEARTBEAT_EVENT_TYPE
) -> datetime | None:
    """Return the most recent heartbeat timestamp for *agent_id*, or None."""
    url = (
        f"{hub}/agents/{agent_id}/agentbus/broadcast"
        f"?event_type={event_type}&limit=1&after_sequence=0"
    )
    # Also check all event types — any sign of life counts
    url_any = f"{hub}/agents/{agent_id}/agentbus/broadcast?limit=1&after_sequence=0"
    for query_url in (url, url_any):
        try:
            events = _get_json(query_url, token)
            if events and isinstance(events, list):
                ts_raw = events[-1].get("created_at") or events[-1].get("timestamp", "")
                if ts_raw:
                    return _parse_iso(ts_raw)
        except RuntimeError:
            pass
    return None


def _parse_iso(s: str) -> datetime | None:
    try:
        # Python 3.11+ fromisoformat handles 'Z' suffix
        s = s.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, AttributeError):
        return None


def _publish_silent_alert(
    hub: str,
    token: str,
    watchdog_agent: str,
    silent_agent: str,
    last_seen: datetime | None,
    threshold_hours: float,
) -> None:
    """Publish a fleet-visible alert that *silent_agent* has gone quiet."""
    now_iso = datetime.now(tz=timezone.utc).isoformat()
    payload: dict[str, Any] = {
        "schema": SILENT_ALERT_SCHEMA,
        "watchdog_agent": watchdog_agent,
        "silent_agent": silent_agent,
        "threshold_hours": threshold_hours,
        "last_seen": last_seen.isoformat() if last_seen else None,
        "checked_at": now_iso,
    }
    body = {
        "agent_id": watchdog_agent,
        "event_type": SILENT_ALERT_EVENT_TYPE,
        "payload": payload,
    }
    _post_json(f"{hub}/agentbus/broadcast", body, token)


# ---------------------------------------------------------------------------
# Public watchdog API
# ---------------------------------------------------------------------------


class AgentStatus:
    """Result for a single agent from the watchdog check."""

    def __init__(
        self,
        agent_id: str,
        last_seen: datetime | None,
        hours_silent: float | None,
        is_silent: bool,
    ) -> None:
        self.agent_id = agent_id
        self.last_seen = last_seen
        self.hours_silent = hours_silent
        self.is_silent = is_silent

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"AgentStatus({self.agent_id!r}, last_seen={self.last_seen}, "
            f"hours_silent={self.hours_silent:.1f}, is_silent={self.is_silent})"
            if self.hours_silent is not None
            else f"AgentStatus({self.agent_id!r}, never_seen, is_silent={self.is_silent})"
        )


def check_silent_agents(
    *,
    hub: str = _DEFAULT_HUB,
    token: str = "",
    watchdog_agent: str = _DEFAULT_AGENT,
    threshold_hours: float = _DEFAULT_THRESHOLD_HOURS,
    alert: bool = True,
    agents: list[str] | None = None,
) -> list[AgentStatus]:
    """Check all (or the specified) registered agents for recent bus activity.

    Returns a list of :class:`AgentStatus` for every agent checked.  If
    *alert* is True, publishes a ``agent.silent.alert.v1`` event for each
    agent that exceeds the silence threshold.

    Parameters
    ----------
    hub:
        MAC hub base URL.
    token:
        Bearer token for hub auth.
    watchdog_agent:
        The agent_id that will publish alert events (must match the token).
    threshold_hours:
        How long (in hours) with no broadcast activity before an agent is
        considered silent.
    alert:
        Whether to publish ``agent.silent.alert.v1`` events for silent agents.
    agents:
        Explicit list of agent IDs to check.  If None, the hub's /agents
        endpoint is queried.
    """
    hub = hub.rstrip("/")
    now = datetime.now(tz=timezone.utc)

    if agents is None:
        agents = _list_registered_agents(hub, token)

    results: list[AgentStatus] = []
    for agent_id in agents:
        last_seen = _latest_broadcast_timestamp(hub, token, agent_id)
        if last_seen is None:
            hours_silent = None
            is_silent = True
        else:
            delta = now - last_seen
            hours_silent = delta.total_seconds() / 3600.0
            is_silent = hours_silent > threshold_hours

        status = AgentStatus(agent_id, last_seen, hours_silent, is_silent)
        results.append(status)

        if is_silent and alert and watchdog_agent and token:
            try:
                _publish_silent_alert(
                    hub, token, watchdog_agent, agent_id, last_seen, threshold_hours
                )
            except RuntimeError:
                pass  # alert failure is non-fatal

    return results


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def _cmd_heartbeat(args: argparse.Namespace, cfg: dict[str, str]) -> int:
    agent_id = args.agent or cfg["agent"]
    if not agent_id:
        print("ERROR: --agent is required", file=sys.stderr)
        return 1
    hub = args.hub or cfg["hub"]
    token = args.token or cfg["token"]
    if not token:
        print("ERROR: MAC_WORKER_TOKEN not set and no --token supplied", file=sys.stderr)
        return 1
    try:
        result = publish_heartbeat(agent_id, hub=hub, token=token, note=args.note)
        print(json.dumps(result, indent=2))
        return 0
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def _cmd_watchdog(args: argparse.Namespace, cfg: dict[str, str]) -> int:
    hub = args.hub or cfg["hub"]
    token = args.token or cfg["token"]
    watchdog_agent = args.agent or cfg["agent"]
    threshold = args.threshold_hours
    agent_list = args.agents.split(",") if args.agents else None

    if not token:
        print("ERROR: MAC_WORKER_TOKEN not set and no --token supplied", file=sys.stderr)
        return 1

    try:
        statuses = check_silent_agents(
            hub=hub,
            token=token,
            watchdog_agent=watchdog_agent,
            threshold_hours=threshold,
            alert=not args.no_alert,
            agents=agent_list,
        )
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    any_silent = False
    for s in statuses:
        if s.is_silent:
            any_silent = True
            last = s.last_seen.isoformat() if s.last_seen else "never"
            hours = f"{s.hours_silent:.1f}h" if s.hours_silent is not None else "unknown"
            print(f"SILENT  {s.agent_id:40s}  last_seen={last}  silent_for={hours}")
        else:
            hours = f"{s.hours_silent:.1f}h" if s.hours_silent is not None else "?"
            print(
                f"ok      {s.agent_id:40s}  last_seen={s.last_seen.isoformat() if s.last_seen else '?'}  age={hours}"
            )

    if any_silent:
        print("\n[watchdog] Silent agents detected.", file=sys.stderr)
        return 2  # non-zero so cron wrappers can detect and escalate
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fleet agent liveness: heartbeat publisher + silent-agent watchdog"
    )
    parser.add_argument("--hub", default="", help="MAC hub URL (overrides env)")
    parser.add_argument("--token", default="", help="Bearer token (overrides env)")
    parser.add_argument("--agent", default="", help="Agent ID for heartbeat/watchdog")

    sub = parser.add_subparsers(dest="cmd")

    hb = sub.add_parser("heartbeat", help="Publish a heartbeat event")
    hb.add_argument("--note", default="", help="Optional note to include in payload")

    wd = sub.add_parser("watchdog", help="Check for silent agents")
    wd.add_argument(
        "--threshold-hours",
        type=float,
        default=float(os.environ.get("FLEET_LIVENESS_THRESHOLD_HOURS", _DEFAULT_THRESHOLD_HOURS)),
        help=f"Hours of silence before flagging (default {_DEFAULT_THRESHOLD_HOURS})",
    )
    wd.add_argument(
        "--agents",
        default="",
        help="Comma-separated list of agent IDs to check (default: all registered)",
    )
    wd.add_argument(
        "--no-alert",
        action="store_true",
        help="Suppress publishing silent-alert events (dry run)",
    )

    args = parser.parse_args()
    if not args.cmd:
        parser.print_help()
        return 0

    cfg = _get_config()

    if args.cmd == "heartbeat":
        return _cmd_heartbeat(args, cfg)
    if args.cmd == "watchdog":
        return _cmd_watchdog(args, cfg)

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
