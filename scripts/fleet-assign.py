#!/usr/bin/env python3
"""
fleet-assign.py — Cross-agent TODO and message injection.

Writes soul_nodes directly into another agent's graph in agent_brain.
The node appears in their soul_prime output at next session start,
tagged [inbox] or [todo] as appropriate.

Usage:
  # Assign a TODO to another agent
  python3 scripts/fleet-assign.py todo natasha "Fix agentfs paths: cd /tmp/mac-agentfs-fix && make lint && git commit && git push"

  # Send a message (injected as [inbox] node)
  python3 scripts/fleet-assign.py message rocky "PR #1025 merged. soul_pg_sync superseded by SoulGraphPG. Update your soul_mcp args."

  # Assign with priority
  python3 scripts/fleet-assign.py todo natasha "..." --priority urgent

  # List another agent's current TODOs/inbox
  python3 scripts/fleet-assign.py list natasha

  # Mark a node done (removes [todo], adds [done])
  python3 scripts/fleet-assign.py done natasha <node_id>

The receiving agent sees these nodes because soul_mcp loads all nodes
WHERE agent = <their_name>. The [inbox]/[todo] tags surface via soul_prime
context-guided hops and soul_by_tag('inbox').

Reto directive 2026-11-07: agents should be able to inject messages and
TODOs into each other's next turns.
"""

from __future__ import annotations
import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


AGENTS = {"bullwinkle", "natasha", "rocky"}
PG_URL = "postgresql://fleet_agent@100.72.16.110:5432/agent_brain"


def _psycopg():
    try:
        import psycopg  # type: ignore[import]

        return psycopg
    except ImportError:
        print(
            "[error] psycopg not found. Run: cd /home/jkh/.mac/src/mac && source .venv/bin/activate",
            file=sys.stderr,
        )
        sys.exit(1)


def _node_id(kind: str, to_agent: str, from_agent: str, content: str) -> str:
    key = f"{kind}:{to_agent}:{from_agent}:{content[:80]}"
    return f"{kind}_" + hashlib.sha256(key.encode()).hexdigest()[:10]


def cmd_todo(to_agent: str, content: str, from_agent: str, priority: str) -> None:
    ps = _psycopg()
    now = datetime.now(timezone.utc)
    node_id = _node_id("todo", to_agent, from_agent, content)

    full_content = f"""## TODO (assigned by {from_agent}, {now.strftime("%Y-%m-%d")})

{content}

assigned_by: {from_agent}
priority: {priority}
status: open"""

    tags = ["todo", "inbox", "fleet"]
    if priority == "urgent":
        tags.append("urgent")

    meta = {
        "kind": "todo",
        "assigned_by": from_agent,
        "priority": priority,
        "visibility": "fleet",
        "status": "open",
    }

    with ps.connect(PG_URL) as conn:
        conn.execute(
            """
            INSERT INTO soul_nodes
              (id, content, tags, parents, pinned, score, created_at, updated_at,
               last_accessed, access_count, metadata, agent)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
              content=EXCLUDED.content, tags=EXCLUDED.tags,
              updated_at=now(), metadata=EXCLUDED.metadata
        """,
            (
                node_id,
                full_content,
                tags,
                [],
                True,
                2.0,
                now,
                now,
                now,
                1,
                json.dumps(meta),
                to_agent,
            ),
        )

    print(f"✅ TODO assigned to {to_agent}: {node_id}")
    print(f"   Content: {content[:100]}")
    print(f"   They'll see it at next soul_prime or soul_by_tag('todo')")


def cmd_message(to_agent: str, content: str, from_agent: str) -> None:
    ps = _psycopg()
    now = datetime.now(timezone.utc)
    node_id = _node_id("msg", to_agent, from_agent, content)

    full_content = f"""## Message from {from_agent} → {to_agent} ({now.strftime("%Y-%m-%d %H:%M")} UTC)

{content}

from: {from_agent}
status: unread"""

    meta = {
        "kind": "message",
        "from_agent": from_agent,
        "to_agent": to_agent,
        "visibility": "fleet",
        "status": "unread",
    }

    with ps.connect(PG_URL) as conn:
        conn.execute(
            """
            INSERT INTO soul_nodes
              (id, content, tags, parents, pinned, score, created_at, updated_at,
               last_accessed, access_count, metadata, agent)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET
              content=EXCLUDED.content, updated_at=now(), metadata=EXCLUDED.metadata
        """,
            (
                node_id,
                full_content,
                ["inbox", "fleet", "message"],
                [],
                False,
                1.5,
                now,
                now,
                now,
                1,
                json.dumps(meta),
                to_agent,
            ),
        )

    print(f"📨 Message injected into {to_agent}'s soul graph: {node_id}")
    print(f"   They'll see it at next soul_prime or soul_by_tag('inbox')")


def cmd_list(agent: str) -> None:
    ps = _psycopg()
    with ps.connect(PG_URL) as conn:
        rows = conn.execute(
            """
            SELECT id, tags, metadata->>'priority', metadata->>'assigned_by',
                   metadata->>'status', left(content, 120)
            FROM soul_nodes
            WHERE agent = %s
              AND (
                'todo'=ANY(tags) OR 'inbox'=ANY(tags)
              )
              AND NOT 'done'=ANY(tags)
            ORDER BY score DESC, created_at DESC
        """,
            (agent,),
        ).fetchall()

    if not rows:
        print(f"No open todos/inbox for {agent}.")
        return

    print(f"\n{agent}'s open TODOs and inbox ({len(rows)} items):")
    for r in rows:
        node_id, tags, priority, assigned_by, status, preview = r
        tag_str = ",".join(t for t in (tags or []) if t not in ("fleet",))
        pri = f"[{priority}] " if priority else ""
        frm = f" (from {assigned_by})" if assigned_by else ""
        print(f"\n  {pri}{node_id}{frm}  [{tag_str}]")
        print(f"  {preview.strip()[:120]}")


def cmd_done(agent: str, node_id: str) -> None:
    ps = _psycopg()
    with ps.connect(PG_URL) as conn:
        conn.execute(
            """
            UPDATE soul_nodes
            SET tags = array_remove(array_remove(tags, 'todo'), 'inbox') || ARRAY['done'],
                pinned = false,
                score = 0.5,
                updated_at = now(),
                metadata = metadata || '{"status":"done"}'::jsonb
            WHERE id = %s AND agent = %s
        """,
            (node_id, agent),
        )
    print(f"✓ Marked done: {node_id} for {agent}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-agent TODO and message injection")
    parser.add_argument("--from-agent", default="bullwinkle", help="Sending agent")
    sub = parser.add_subparsers(dest="cmd")

    t = sub.add_parser("todo", help="Assign a TODO to an agent")
    t.add_argument("to_agent", choices=AGENTS)
    t.add_argument("content", help="TODO description")
    t.add_argument("--priority", default="normal", choices=["normal", "urgent"])

    m = sub.add_parser("message", help="Inject a message into an agent's inbox")
    m.add_argument("to_agent", choices=AGENTS)
    m.add_argument("content", help="Message text")

    ls = sub.add_parser("list", help="List open TODOs/inbox for an agent")
    ls.add_argument("agent", choices=AGENTS)

    d = sub.add_parser("done", help="Mark a TODO done")
    d.add_argument("agent", choices=AGENTS)
    d.add_argument("node_id")

    args = parser.parse_args()
    from_agent = args.from_agent

    if args.cmd == "todo":
        cmd_todo(args.to_agent, args.content, from_agent, args.priority)
    elif args.cmd == "message":
        cmd_message(args.to_agent, args.content, from_agent)
    elif args.cmd == "list":
        cmd_list(args.agent)
    elif args.cmd == "done":
        cmd_done(args.agent, args.node_id)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
