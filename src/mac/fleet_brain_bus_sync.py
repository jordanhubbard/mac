"""fleet_brain_bus_sync — poll the MAC AgentBus broadcast feed and mirror events
into the local natasha-pg ``agent_messages`` table.

Usage::

    python -m mac.fleet_brain_bus_sync [--limit N] [--reset-cursor]

Environment (all optional — falls back to ~/.mac/mac.env):

    MAC_HUB_URL          — hub API base URL (default http://100.72.16.110:8789)
    MAC_WORKER_TOKEN     — bearer token for hub auth
    NATASHA_PG_DSN       — local PG connection string
                           (default dbname=natasha user=natasha host=localhost port=5433)

The cursor (last consumed sequence number) is persisted in
``cron_job_state.metadata -> last_sequence`` under job_name ``agentbus_broadcast_sync``
so each run picks up only new events.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import psycopg2
import psycopg2.extras

from mac.mac_paths import mac_env_file

# ── Config ────────────────────────────────────────────────────────────────────

_ENV_FILE = mac_env_file()
_JOB_NAME = "agentbus_broadcast_sync"
_AGENT_ID = "agent_natasha"
_DEFAULT_DSN = "dbname=natasha user=natasha host=localhost port=5433"
_DEFAULT_HUB = "http://100.72.16.110:8789"


def _load_mac_env() -> Dict[str, str]:
    env: Dict[str, str] = {}
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
            if k in env or k.startswith("MAC_") or k.startswith("NATASHA_")
        }
    )
    return env


def _get_token(env: Dict[str, str]) -> str:
    for k in ("MAC_WORKER_TOKEN__MAC", "MAC_API_TOKEN__MAC", "MAC_WORKER_TOKEN", "MAC_API_TOKEN"):
        if env.get(k):
            return env[k]
    return ""


# ── Cursor persistence ────────────────────────────────────────────────────────


def _read_cursor(conn: Any) -> int:
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute(
            "SELECT metadata FROM cron_job_state WHERE job_name = %s",
            (_JOB_NAME,),
        )
        row = cur.fetchone()
    if row and row["metadata"] and "last_sequence" in row["metadata"]:
        return int(row["metadata"]["last_sequence"])
    return 0


def _write_cursor(conn: Any, sequence: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO cron_job_state (job_name, last_run_at, run_count, metadata)
               VALUES (%s, NOW(), 1, %s)
               ON CONFLICT (job_name) DO UPDATE
               SET last_run_at = NOW(),
                   run_count   = cron_job_state.run_count + 1,
                   metadata    = cron_job_state.metadata || %s::jsonb
            """,
            (
                _JOB_NAME,
                psycopg2.extras.Json(
                    {
                        "last_sequence": sequence,
                        "last_sync_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
                psycopg2.extras.Json(
                    {
                        "last_sequence": sequence,
                        "last_sync_at": datetime.now(timezone.utc).isoformat(),
                    }
                ),
            ),
        )
    conn.commit()


# ── Hub polling ───────────────────────────────────────────────────────────────


def _fetch_broadcasts(
    hub: str, token: str, after_sequence: int, limit: int
) -> List[Dict[str, Any]]:
    url = (
        f"{hub}/agents/{_AGENT_ID}/agentbus/broadcast?after_sequence={after_sequence}&limit={limit}"
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:300].decode(errors="replace")
        raise RuntimeError(f"hub broadcast HTTP {exc.code}: {body}") from exc


# ── Write to agent_messages ───────────────────────────────────────────────────


def _upsert_message(conn: Any, event: Dict[str, Any]) -> bool:
    """Insert one broadcast event; returns True if new, False if already present."""
    msg_id = f"broadcast:{event['sequence']}"
    sender = event.get("agent_id") or "hub"
    topic = event.get("event_type", "unknown")
    payload = event.get("payload") or {}
    task_id = event.get("task_id")
    if task_id:
        payload = dict(payload, task_id=task_id)
    project = event.get("project")
    if project:
        payload = dict(payload, project=project)
    sent_at = event.get("created_at")

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO agent_messages
               (message_id, sender, recipient, topic, payload, message_type, sent_at, logged_at)
               VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
               ON CONFLICT (message_id) DO NOTHING
            """,
            (
                msg_id,
                sender,
                _AGENT_ID,
                topic,
                psycopg2.extras.Json(payload),
                "agentbus_broadcast",
                sent_at,
            ),
        )
        inserted = cur.rowcount > 0
    conn.commit()
    return inserted


# ── Main ──────────────────────────────────────────────────────────────────────


def sync(limit: int = 200, reset_cursor: bool = False) -> Dict[str, Any]:
    env = _load_mac_env()
    hub = env.get("MAC_HUB_URL", _DEFAULT_HUB)
    token = _get_token(env)
    dsn = env.get("NATASHA_PG_DSN", _DEFAULT_DSN)

    if not token:
        return {"ok": False, "error": "no MAC token found in mac.env or environment"}

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:
        return {"ok": False, "error": f"PG connect failed: {exc}"}

    after = 0 if reset_cursor else _read_cursor(conn)

    try:
        events = _fetch_broadcasts(hub, token, after_sequence=after, limit=limit)
    except Exception as exc:
        conn.close()
        return {"ok": False, "error": str(exc)}

    inserted = 0
    skipped = 0
    max_seq = after
    for event in events:
        seq = event.get("sequence", 0)
        if seq > max_seq:
            max_seq = seq
        new = _upsert_message(conn, event)
        if new:
            inserted += 1
        else:
            skipped += 1

    if max_seq > after:
        _write_cursor(conn, max_seq)

    conn.close()
    return {
        "ok": True,
        "fetched": len(events),
        "inserted": inserted,
        "skipped": skipped,
        "cursor_before": after,
        "cursor_after": max_seq,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync AgentBus broadcasts → agent_messages")
    ap.add_argument("--limit", type=int, default=200, help="max events per run")
    ap.add_argument("--reset-cursor", action="store_true", help="re-sync from sequence 0")
    args = ap.parse_args()

    result = sync(limit=args.limit, reset_cursor=args.reset_cursor)
    if result["ok"]:
        print(
            f"[fleet_brain_bus_sync] ok: fetched={result['fetched']} "
            f"inserted={result['inserted']} skipped={result['skipped']} "
            f"cursor={result['cursor_before']}→{result['cursor_after']}"
        )
        return 0
    else:
        print(f"[fleet_brain_bus_sync] ERROR: {result['error']}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
