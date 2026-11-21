"""fleet_brain_bus_sync - poll the MAC AgentBus broadcast feed and mirror events
into the local fleet_brain ``bus_messages`` table.

Usage::

    python -m mac.fleet_brain_bus_sync [--limit N] [--reset-cursor]
                                       [--agent AGENT_ID]

Environment (all optional -- falls back to ~/.mac/mac.env):

    MAC_HUB_URL          -- hub API base URL (default http://100.72.16.110:8789)
    MAC_WORKER_TOKEN     -- bearer token for hub auth
    FLEET_BRAIN_DSN      -- local PG connection string for fleet_brain
                           (default: connects as the local postgres Unix socket
                           using the ``bullwinkle`` role, database ``fleet_brain``)
    FLEET_BUS_AGENT      -- the agent whose cursor to track
                           (default: ``bullwinkle``)

The cursor (last consumed bus_message row ID) is persisted in
``agent_cursors.last_read_id`` for the configured agent so each run
picks up only new events.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

import psycopg
from psycopg.rows import dict_row

from mac.mac_paths import mac_env_file

# -- Config -------------------------------------------------------------------

_ENV_FILE = mac_env_file()
_DEFAULT_HUB = "http://100.72.16.110:8789"
_DEFAULT_AGENT = "bullwinkle"
# fleet_brain lives on this node (Bullwinkle / madmax); connect via Unix socket
# or via psycopg DSN if FLEET_BRAIN_DSN is set.
_DEFAULT_DSN = "dbname=fleet_brain"


def _load_mac_env() -> dict[str, str]:
    env: dict[str, str] = {}
    if _ENV_FILE.exists():
        for line in _ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    env.update(
        {
            k: v
            for k, v in os.environ.items()
            if k in env or k.startswith("MAC_") or k.startswith("FLEET_")
        }
    )
    return env


def _get_token(env: dict[str, str]) -> str:
    for k in ("MAC_WORKER_TOKEN__MAC", "MAC_API_TOKEN__MAC", "MAC_WORKER_TOKEN", "MAC_API_TOKEN"):
        if env.get(k):
            return env[k]
    return ""


# -- Hub polling --------------------------------------------------------------


def _fetch_broadcasts(
    hub: str, token: str, agent_id: str, after_sequence: int, limit: int
) -> list[dict[str, Any]]:
    url = (
        f"{hub}/agents/{agent_id}/agentbus/broadcast?after_sequence={after_sequence}&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode(errors="replace")
        raise RuntimeError(f"hub broadcast HTTP {exc.code}: {body}") from exc


# -- Cursor persistence -------------------------------------------------------


def _read_cursor(conn: Any, agent_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT last_read_id FROM agent_cursors WHERE agent_id = %s",
            (agent_id,),
        )
        row = cur.fetchone()
    return int(row["last_read_id"]) if row and row.get("last_read_id") else 0


def _write_cursor(conn: Any, agent_id: str, last_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_cursors (agent_id, last_read_id, updated_at)
               VALUES (%s, %s, NOW())
               ON CONFLICT (agent_id) DO UPDATE
               SET last_read_id = EXCLUDED.last_read_id,
                   updated_at   = NOW()
            """,
            (agent_id, last_id),
        )
    conn.commit()


# -- Write to bus_messages ----------------------------------------------------

# FK constraint: from_agent / to_agent must reference agents(id).
# Hub events have agent_id values like "agent_bullwinkle"; fleet_brain uses short
# IDs like "bullwinkle". We normalise by stripping the "agent_" prefix.
_REGISTERED = frozenset({"bullwinkle", "rocky", "natasha", "asa"})


def _normalise_agent(raw: str | None) -> str | None:
    if not raw:
        return None
    normalised = raw.removeprefix("agent_")
    return normalised if normalised in _REGISTERED else None


def _upsert_message(conn: Any, event: dict[str, Any]) -> bool:
    """Insert one broadcast event into bus_messages; returns True if new."""
    # Build a stable content column: the full broadcast JSON as text.
    content = json.dumps(event, separators=(",", ":"), sort_keys=True)
    from_agent = _normalise_agent(event.get("agent_id"))
    # Broadcasts have no explicit recipient; to_agent stays NULL.
    sent_at = event.get("created_at")
    metadata = {
        "sequence": event.get("sequence"),
        "event_type": event.get("event_type"),
        "task_id": event.get("task_id"),
        "project": event.get("project"),
    }
    # Idempotency: use metadata->>'sequence' to avoid duplicates.
    sequence = event.get("sequence", 0)

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO bus_messages
               (from_agent, to_agent, content, content_type, metadata, sent_at)
               SELECT %s, NULL, %s, 'application/json', %s, %s
               WHERE NOT EXISTS (
                   SELECT 1 FROM bus_messages
                   WHERE metadata->>'sequence' = %s
               )
            """,
            (
                from_agent,
                content,
                json.dumps(metadata),
                sent_at,
                str(sequence),
            ),
        )
        inserted = cur.rowcount > 0
    conn.commit()
    return inserted


# -- Main sync loop -----------------------------------------------------------


def sync(
    limit: int = 200,
    reset_cursor: bool = False,
    agent_id: str = _DEFAULT_AGENT,
) -> dict[str, Any]:
    env = _load_mac_env()
    hub = env.get("MAC_HUB_URL", _DEFAULT_HUB)
    token = _get_token(env)
    dsn = env.get("FLEET_BRAIN_DSN", _DEFAULT_DSN)

    # Hub agent ID format for the broadcast endpoint
    hub_agent_id = agent_id if agent_id.startswith("agent_") else f"agent_{agent_id}"

    if not token:
        return {"ok": False, "error": "no MAC token found in mac.env or environment"}

    try:
        conn = psycopg.connect(dsn, row_factory=dict_row)
    except psycopg.OperationalError as exc:
        return {"ok": False, "error": f"PG connect failed: {exc}"}

    after = 0 if reset_cursor else _read_cursor(conn, agent_id)

    try:
        events = _fetch_broadcasts(hub, token, hub_agent_id, after_sequence=after, limit=limit)
    except Exception as exc:
        conn.close()
        return {"ok": False, "error": str(exc)}

    inserted = 0
    skipped = 0
    max_seq = after
    last_bus_id: int | None = None

    for event in events:
        seq = event.get("sequence", 0)
        if seq > max_seq:
            max_seq = seq
        new = _upsert_message(conn, event)
        if new:
            inserted += 1
            # Track the latest bus_messages.id for the cursor
            with conn.cursor() as cur:
                cur.execute("SELECT MAX(id) AS max_id FROM bus_messages")
                row = cur.fetchone()
            if row and row.get("max_id"):
                last_bus_id = int(row["max_id"])
        else:
            skipped += 1

    if last_bus_id is not None:
        _write_cursor(conn, agent_id, last_bus_id)
    conn.close()

    return {
        "ok": True,
        "fetched": len(events),
        "inserted": inserted,
        "skipped": skipped,
        "cursor_before": after,
        "cursor_after": last_bus_id or after,
        "max_sequence": max_seq,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync AgentBus broadcasts -> fleet_brain.bus_messages")
    ap.add_argument("--limit", type=int, default=200, help="max events per run")
    ap.add_argument("--reset-cursor", action="store_true", help="re-sync from sequence 0")
    ap.add_argument(
        "--agent",
        default=_DEFAULT_AGENT,
        help="agent whose cursor to track (default: bullwinkle)",
    )
    args = ap.parse_args()

    result = sync(limit=args.limit, reset_cursor=args.reset_cursor, agent_id=args.agent)
    if result["ok"]:
        print(
            f"[fleet_brain_bus_sync] ok: fetched={result['fetched']} "
            f"inserted={result['inserted']} skipped={result['skipped']} "
            f"cursor={result['cursor_before']}-->{result['cursor_after']} "
            f"max_seq={result['max_sequence']}"
        )
        return 0
    else:
        print(f"[fleet_brain_bus_sync] ERROR: {result['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
