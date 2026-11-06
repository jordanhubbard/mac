#!/usr/bin/env python3
"""
fleet_brain_bus_sync.py — AgentBus broadcast → fleet_brain.bus_messages sync.

Polls the MAC AgentBus broadcast feed (via mac CLI) and mirrors new events
into the local fleet_brain.bus_messages table. Maintains per-agent cursor
in agent_cursors so each run resumes from where it left off.

Agent identity: agent_bullwinkle (this is Bullwinkle's node).
Task: task_0108d20c9d23490591b8838347b9d9f2

Usage:
    python scripts/fleet_brain_bus_sync.py [--limit N] [--dry-run]

Runs clean from cron or one-shot.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

AGENT_ID = "agent_bullwinkle"
DB_NAME = "fleet_brain"
MAC_ENV = os.path.expanduser("~/.mac/mac.env")

# Mapping: hub agent_id → fleet_brain agents.id
# Hub uses "agent_bullwinkle", "agent_rocky", etc.
# fleet_brain uses "bullwinkle", "rocky", etc.
HUB_TO_LOCAL_AGENT_ID: dict[str, str] = {
    "agent_bullwinkle": "bullwinkle",
    "agent_rocky": "rocky",
    "agent_natasha": "natasha",
    "agent_asa": "asa",
    "hub": None,  # hub system events — not an agent in fleet_brain
}


def load_mac_env() -> dict[str, str]:
    """Source ~/.mac/mac.env and return the vars it sets."""
    result = {}
    if not os.path.exists(MAC_ENV):
        return result
    try:
        out = subprocess.check_output(
            ["bash", "-c", f"source {MAC_ENV} && env"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            if "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    except subprocess.CalledProcessError:
        pass
    return result


def get_cursor(env: dict[str, str]) -> int:
    """Read Bullwinkle's last_read_id from fleet_brain.agent_cursors."""
    try:
        out = subprocess.check_output(
            [
                "psql",
                "-d", DB_NAME,
                "-t", "-A",
                "-c", f"SELECT last_read_id FROM agent_cursors WHERE agent_id = 'bullwinkle';",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        val = out.strip()
        return int(val) if val else 0
    except (subprocess.CalledProcessError, ValueError):
        return 0


def set_cursor(sequence: int) -> None:
    """Update or insert the cursor for bullwinkle."""
    sql = (
        "INSERT INTO agent_cursors (agent_id, last_read_id, updated_at) "
        "VALUES ('bullwinkle', %(seq)s, now()) "
        "ON CONFLICT (agent_id) DO UPDATE SET last_read_id = %(seq)s, updated_at = now();"
    )
    subprocess.run(
        ["psql", "-d", DB_NAME, "-c", sql % {"seq": sequence}],
        capture_output=True,
        check=True,
    )


def fetch_broadcasts(after_sequence: int, limit: int, env: dict[str, str]) -> list[dict[str, Any]]:
    """Call mac admin agentbus broadcast and return parsed JSON."""
    token = env.get("MAC_WORKER_TOKEN", "")
    run_env = {**os.environ, "MAC_API_TOKEN": token}
    cmd = [
        "mac", "admin", "agentbus", "broadcast",
        AGENT_ID,
        "--after-sequence", str(after_sequence),
        "--limit", str(limit),
    ]
    try:
        out = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL, env=run_env)
        return json.loads(out)
    except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"[bus-sync] fetch failed: {exc}", file=sys.stderr)
        return []


def insert_messages(events: list[dict[str, Any]], dry_run: bool) -> int:
    """Insert events into fleet_brain.bus_messages. Returns count inserted."""
    if not events:
        return 0
    inserted = 0
    for ev in events:
        hub_agent = str(ev.get("agent_id") or "")
        from_agent = HUB_TO_LOCAL_AGENT_ID.get(hub_agent)
        # Skip events from hub system or unknown agents (FK constraint)
        if from_agent is None and hub_agent not in ("hub",):
            # Unknown agent — store as None (to_agent FK is nullable)
            from_agent = None

        content = json.dumps(ev)
        sent_at = ev.get("created_at") or datetime.now(timezone.utc).isoformat()
        event_type = str(ev.get("event_type") or "")
        sequence = int(ev.get("sequence") or 0)

        # Build the SQL — escape single quotes in content
        content_escaped = content.replace("'", "''")
        from_agent_sql = f"'{from_agent}'" if from_agent else "NULL"
        sql = (
            f"INSERT INTO bus_messages (from_agent, to_agent, content, content_type, metadata, sent_at) "
            f"VALUES ({from_agent_sql}, NULL, '{content_escaped}', 'application/json', "
            f"'{{\"event_type\": \"{event_type}\", \"sequence\": {sequence}}}'::jsonb, "
            f"'{sent_at}') "
            f"ON CONFLICT DO NOTHING;"
        )
        if dry_run:
            print(f"[DRY] would insert seq={sequence} type={event_type} from={from_agent}")
            inserted += 1
            continue
        result = subprocess.run(
            ["psql", "-d", DB_NAME, "-c", sql],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and "INSERT 0 1" in result.stdout:
            inserted += 1
        elif result.returncode != 0:
            print(f"[bus-sync] insert error seq={sequence}: {result.stderr.strip()}", file=sys.stderr)
    return inserted


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync AgentBus broadcasts → fleet_brain.bus_messages")
    parser.add_argument("--limit", type=int, default=100, help="Max events to fetch per run")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print, do not write")
    args = parser.parse_args()

    env = load_mac_env()
    if not env.get("MAC_WORKER_TOKEN"):
        print("[bus-sync] WARNING: MAC_WORKER_TOKEN not found in mac.env", file=sys.stderr)

    cursor = get_cursor(env)
    print(f"[bus-sync] cursor={cursor}, limit={args.limit}, dry_run={args.dry_run}")

    events = fetch_broadcasts(after_sequence=cursor, limit=args.limit, env=env)
    print(f"[bus-sync] fetched {len(events)} events from hub")

    if not events:
        print("[bus-sync] nothing new, done")
        return

    max_seq = max(int(ev.get("sequence") or 0) for ev in events)
    inserted = insert_messages(events, dry_run=args.dry_run)
    print(f"[bus-sync] inserted {inserted}/{len(events)}, max_sequence={max_seq}")

    if not args.dry_run and events:
        set_cursor(max_seq)
        print(f"[bus-sync] cursor advanced to {max_seq}")


if __name__ == "__main__":
    main()
