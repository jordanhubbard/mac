#!/usr/bin/env python3
"""
slack-embed.py — Embed Slack channel messages into agent_brain soul_nodes.

Reads Slack messages from the Hermes gateway session log (available in
~/.hermes/sessions/) and writes them as soul_nodes tagged [slack, inbox]
with metadata:{source:'slack', channel, ts, user, thread_ts}.

This lets agents search conversation history semantically via soul_related()
and soul_query(), and surfaces urgent messages via the [todo]/[inbox] tags.

Usage:
  python3 scripts/slack-embed.py --channel rockyandfriends [--limit 50] [--since <ISO date>]
  python3 scripts/slack-embed.py --session-file <path>   # specific session JSON

Reto directive 2026-11-07: embed Slack messages in DB so agents can inspect
the channel and get context at session start.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def _venv_python():
    mac_src = Path.home() / ".mac" / "src" / "mac"
    return mac_src / ".venv" / "bin" / "python3"


def _pg_url() -> str:
    return "postgresql://fleet_agent@100.72.16.110:5432/agent_brain"


def _msg_node_id(channel: str, ts: str, user: str) -> str:
    key = f"slack:{channel}:{ts}:{user}"
    return "slack_" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _load_sessions(limit: int, since: str | None) -> list[dict]:
    """Find recent Hermes session files and extract Slack messages."""
    sessions_dir = Path.home() / ".hermes" / "sessions"
    messages = []

    if not sessions_dir.exists():
        print(f"[warn] No sessions dir at {sessions_dir}", file=sys.stderr)
        return []

    # Find session files (JSON), most recent first
    session_files = sorted(
        sessions_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )

    for sf in session_files[:20]:  # scan up to 20 session files
        try:
            data = json.loads(sf.read_text())
            # Session files have 'messages' list
            msgs = data if isinstance(data, list) else data.get("messages", [])
            for m in msgs:
                # Look for user messages with slack metadata
                meta = m.get("metadata", {}) or {}
                platform = meta.get("platform", "") or m.get("platform", "")
                if "slack" not in str(platform).lower():
                    continue
                channel = meta.get("channel") or meta.get("chat_id", "unknown")
                ts = meta.get("ts") or meta.get("timestamp") or m.get("created_at", "")
                user = meta.get("user") or meta.get("sender") or m.get("role", "user")
                content = m.get("content", "") or ""
                if not content:
                    continue
                if since and ts and ts < since:
                    continue
                messages.append(
                    {
                        "channel": channel,
                        "ts": str(ts),
                        "user": str(user),
                        "content": str(content),
                        "session_file": str(sf),
                    }
                )
        except Exception:
            continue

    return messages[:limit]


def _load_from_context(limit: int) -> list[dict]:
    """Extract from the current conversation context if passed via stdin."""
    messages = []
    for line in sys.stdin:
        try:
            m = json.loads(line)
            messages.append(m)
        except Exception:
            pass
    return messages[:limit]


def embed_messages(messages: list[dict], agent: str = "bullwinkle", dry_run: bool = False) -> int:
    """Write Slack messages as soul_nodes to agent_brain. Returns count written."""
    if not messages:
        print("No messages to embed.")
        return 0

    try:
        import psycopg  # type: ignore[import]
    except ImportError:
        print("[error] psycopg not available. Run from mac repo venv.", file=sys.stderr)
        sys.exit(1)

    pg_url = _pg_url()
    now = datetime.now(timezone.utc)
    written = 0

    with psycopg.connect(pg_url) as conn:
        # Check existing IDs to avoid dupes
        existing = set(
            r[0]
            for r in conn.execute(
                "SELECT id FROM soul_nodes WHERE agent=%s AND 'slack'=ANY(tags)", (agent,)
            ).fetchall()
        )

        for m in messages:
            channel = m.get("channel", "unknown")
            ts = m.get("ts", "")
            user = m.get("user", "unknown")
            content = m.get("content", "")

            node_id = _msg_node_id(channel, ts, user)
            if node_id in existing:
                continue

            # Determine urgency tags
            extra_tags = []
            content_lower = content.lower()
            if any(w in content_lower for w in ["urgent", "asap", "blocked", "help", "emergency"]):
                extra_tags.append("urgent")
            if any(w in content_lower for w in ["todo", "build", "implement", "fix", "needs"]):
                extra_tags.append("todo")

            tags = ["slack", "fleet"] + extra_tags

            # Truncate very long messages for the soul node
            body = content[:2000] if len(content) > 2000 else content
            dt_str = ts[:10] if len(ts) >= 10 else ts
            node_content = f"## Slack #{channel} ({user}, {dt_str})\n\n{body}"

            meta = {
                "source": "slack",
                "channel": channel,
                "ts": ts,
                "user": user,
                "visibility": "fleet",
                "kind": "slack_message",
            }

            if dry_run:
                print(f"  [dry] would write: {node_id} — {user} in #{channel}: {content[:60]}")
                written += 1
                continue

            conn.execute(
                """
                INSERT INTO soul_nodes
                  (id, content, tags, parents, pinned, score, created_at, updated_at,
                   last_accessed, access_count, metadata, agent)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (id) DO NOTHING
            """,
                (
                    node_id,
                    node_content,
                    tags,
                    [],
                    False,
                    1.0,
                    now,
                    now,
                    now,
                    1,
                    json.dumps(meta),
                    agent,
                ),
            )
            print(f"  wrote: {node_id} — {user} in #{channel}: {content[:60]}")
            written += 1

    return written


def embed_current_thread(messages_json: str, channel: str, agent: str) -> int:
    """Embed messages passed as JSON string (e.g. from the current conversation)."""
    try:
        msgs = json.loads(messages_json)
    except Exception as exc:
        print(f"[error] could not parse messages JSON: {exc}", file=sys.stderr)
        return 0
    normalized = []
    for m in msgs:
        normalized.append(
            {
                "channel": channel,
                "ts": m.get("ts") or m.get("timestamp") or datetime.now(timezone.utc).isoformat(),
                "user": m.get("user") or m.get("sender") or m.get("role", "user"),
                "content": m.get("content") or m.get("text") or "",
            }
        )
    return embed_messages(normalized, agent=agent)


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed Slack messages into agent_brain soul_nodes")
    parser.add_argument("--channel", default="rockyandfriends")
    parser.add_argument("--agent", default="bullwinkle")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--since", help="ISO date filter, e.g. 2026-11-01")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--messages-json", help="JSON array of {user,ts,content} to embed directly")
    args = parser.parse_args()

    if args.messages_json:
        n = embed_current_thread(args.messages_json, args.channel, args.agent)
        print(f"Embedded {n} messages from --messages-json.")
        return

    messages = _load_sessions(limit=args.limit, since=args.since)
    if not messages:
        print(
            "No Slack messages found in session files. Try --messages-json to pass them directly."
        )
        print(
            'Example: python3 scripts/slack-embed.py --messages-json \'[{"user":"Reto","ts":"...","content":"..."}]\''
        )
        return

    n = embed_messages(messages, agent=args.agent, dry_run=args.dry_run)
    print(f"\nEmbedded {n} new Slack messages into agent_brain.")


if __name__ == "__main__":
    main()
