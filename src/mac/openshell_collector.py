"""OpenShell event collector helpers.

The Linux collector tails/pulls OpenShell OCSF-style JSON events and posts
normalized ``mac.action_event.v1`` records to the MAC hub. This module keeps the
normalization deterministic and testable; deployment decides whether events are
read from a file, FIFO, or an OpenShell CLI stream.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, Optional, Sequence

from mac.models import utcnow


def normalize_openshell_event(
    raw: Dict[str, Any],
    *,
    agent_id: Optional[str] = None,
    sandbox_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Normalize a raw OpenShell event into the action-event schema."""
    event = raw.get("event") if isinstance(raw.get("event"), dict) else raw
    outcome_raw = str(
        event.get("outcome")
        or event.get("disposition")
        or event.get("action")
        or ""
    ).lower()
    denied = any(marker in outcome_raw for marker in ("deny", "denied", "blocked", "reject"))
    allowed = any(marker in outcome_raw for marker in ("allow", "allowed", "permit"))
    outcome = "denied" if denied else ("allowed" if allowed else "unknown")
    severity = "warning" if denied else "info"
    action_type = str(event.get("category") or event.get("class_name") or event.get("type") or "openshell")
    action_name = str(event.get("name") or event.get("activity_name") or event.get("operation") or "event")
    attrs = dict(event)
    attrs.setdefault("raw", raw)
    policy = event.get("policy") if isinstance(event.get("policy"), dict) else {}
    process = event.get("process") if isinstance(event.get("process"), dict) else {}
    actor = str(event.get("actor") or process.get("name") or "openshell")
    return {
        "event_id": event.get("event_id") or event.get("id"),
        "timestamp": str(event.get("time") or event.get("timestamp") or utcnow()),
        "agent_id": agent_id or event.get("agent_id"),
        "task_id": event.get("task_id"),
        "session_id": event.get("session_id"),
        "sandbox_id": sandbox_id or event.get("sandbox_id") or event.get("container_id"),
        "actor": actor,
        "action_type": "openshell.%s" % action_type.strip().lower().replace(" ", "_"),
        "action_name": action_name.strip().lower().replace(" ", "_"),
        "subject_type": str(event.get("subject_type") or "sandbox"),
        "subject_id": str(event.get("subject_id") or sandbox_id or event.get("sandbox_id") or event.get("container_id") or ""),
        "outcome": outcome,
        "severity": severity,
        "policy_id": policy.get("id") or event.get("policy_id"),
        "policy_version": policy.get("version") or event.get("policy_version"),
        "command_id": event.get("command_id"),
        "parent_event_id": event.get("parent_event_id"),
        "attributes": attrs,
        "redaction_state": "redacted",
    }


def iter_json_lines(path: Path) -> Iterator[Dict[str, Any]]:
    """Yield parsed JSON objects from each non-empty line of the file."""
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def post_action_event(base_url: str, token: str, event: Dict[str, Any], *, timeout: float = 10.0) -> None:
    """Post a single action event to the hub action-events endpoint."""
    data = json.dumps(event, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = urllib.request.Request(
        base_url.rstrip("/") + "/action-events",
        data=data,
        headers={
            "Authorization": "Bearer %s" % token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        method="POST",
    )
    urllib.request.urlopen(request, timeout=timeout).read()  # noqa: S310


def collect_once(
    events: Iterable[Dict[str, Any]],
    *,
    base_url: str,
    token: str,
    agent_id: Optional[str] = None,
    sandbox_id: Optional[str] = None,
) -> int:
    """Normalize and post each event, returning the number sent."""
    count = 0
    for raw in events:
        post_action_event(
            base_url,
            token,
            normalize_openshell_event(raw, agent_id=agent_id, sandbox_id=sandbox_id),
        )
        count += 1
    return count


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the OpenShell collector entry point and return its exit code."""
    parser = argparse.ArgumentParser(prog="mac-openshell-collector")
    parser.add_argument("--events-file", required=True)
    parser.add_argument("--hub-url", default=os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL"))
    parser.add_argument("--token", default=os.environ.get("MAC_WORKER_TOKEN") or os.environ.get("MAC_API_TOKEN"))
    parser.add_argument("--agent-id", default=os.environ.get("MAC_AGENT_ID"))
    parser.add_argument("--sandbox-id", default=os.environ.get("MAC_OPENSHELL_SANDBOX_ID"))
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.hub_url or not args.token:
        sys.stderr.write("MAC hub URL and token are required\n")
        return 2
    path = Path(args.events_file).expanduser()
    if not args.follow:
        print(json.dumps({"posted": collect_once(iter_json_lines(path), base_url=args.hub_url, token=args.token, agent_id=args.agent_id, sandbox_id=args.sandbox_id)}))
        return 0
    seen = 0
    while True:
        rows = list(iter_json_lines(path))
        for raw in rows[seen:]:
            post_action_event(
                args.hub_url,
                args.token,
                normalize_openshell_event(raw, agent_id=args.agent_id, sandbox_id=args.sandbox_id),
            )
        seen = len(rows)
        time.sleep(max(0.25, args.interval))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
