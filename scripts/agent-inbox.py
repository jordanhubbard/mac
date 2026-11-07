#!/usr/bin/env python3
"""
agent-inbox.py — Index unread AgentBus messages as soul graph nodes.

Usage:
  python3 scripts/agent-inbox.py --agent agent_bullwinkle [--limit 20] [--reply <mes_id> "text"]

What it does:
  1. Reads pending AgentBus messages for the agent
  2. For each unread message: writes a soul node tagged [inbox] with full content
  3. Writes compact stubs to ~/.hermes/MEMORY.md fragment so session-start context surfaces them
  4. Reply: publish via AgentBus + mark soul node [replied]

Stubs in memory look like:
  📬 mes_abc12345 (rocky → bullwinkle, 2026-11-07): repo-update request (branch=main)

At session start, soul_prime surfaces inbox-tagged nodes. Full content via soul_splay('mes_abc12345').
"""

from __future__ import annotations
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def _mac_env() -> dict:
    env_path = Path.home() / ".mac" / "mac.env"
    env = dict(os.environ)
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("export "):
                line = line[7:]
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _run(cmd: list[str], env: dict) -> dict:
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {result.stderr[:200]}")
    return json.loads(result.stdout)


def _msg_id(chunk_id: str) -> str:
    """Stable 8-char ID from the chunk ID."""
    return "mes_" + hashlib.sha256(chunk_id.encode()).hexdigest()[:8]


def _stub_line(
    msg_id: str, sender: str, agent: str, created_at: str, topic: str, summary: str
) -> str:
    dt = created_at[:10] if created_at else "?"
    sender_short = sender.replace("agent_", "") if sender else "?"
    agent_short = agent.replace("agent_", "") if agent else "?"
    return f"📬 {msg_id} ({sender_short}→{agent_short}, {dt}): {topic} — {summary[:80]}"


def _soul_add(content: str, tags: list[str], parents: list[str], node_id: str) -> bool:
    """Write a soul node via the soul_mcp CLI (file-backed mode)."""
    soul_path = Path.home() / ".hermes" / "soul.json"
    if not soul_path.exists():
        print(f"  [warn] soul.json not found at {soul_path}", file=sys.stderr)
        return False
    try:
        import sys as _sys

        sys.path.insert(0, str(Path.home() / ".mac" / "src" / "mac" / "src"))
        from mac.soul_graph import SoulGraph  # type: ignore[import]

        g = SoulGraph.load(soul_path)
        # Only add if not already present
        if node_id in g.nodes:
            return False
        g.add(content, tags=set(tags), parents=parents, node_id=node_id)
        g.save(soul_path)
        return True
    except Exception as exc:
        print(f"  [warn] soul_add failed: {exc}", file=sys.stderr)
        return False


def _memory_stub_path() -> Path:
    return Path.home() / ".hermes" / "inbox_stubs.md"


def _load_known_ids() -> set[str]:
    p = _memory_stub_path()
    if not p.exists():
        return set()
    ids = set()
    for line in p.read_text().splitlines():
        if line.startswith("📬 mes_"):
            msg_id = line.split()[1]
            ids.add(msg_id)
    return ids


def _append_stub(stub: str) -> None:
    p = _memory_stub_path()
    with open(p, "a") as f:
        f.write(stub + "\n")


def _extract_summary(payload) -> str:
    if isinstance(payload, str):
        return payload[:120].replace("\n", " ")
    if isinstance(payload, dict):
        for key in ("summary", "message", "body", "content", "topic", "request_id"):
            if key in payload and payload[key]:
                return str(payload[key])[:120].replace("\n", " ")
        return str(payload)[:120].replace("\n", " ")
    return str(payload)[:120]


def cmd_read(agent: str, limit: int, env: dict) -> None:
    """Read pending messages, index new ones as soul nodes + memory stubs."""
    data = _run(["mac", "admin", "agentbus", "wait", agent, "--timeout", "1"], env)
    messages = data.get("messages", [])[:limit]

    known = _load_known_ids()
    new_count = 0

    for msg in messages:
        chunk_id = msg.get("id", "")
        msg_id = _msg_id(chunk_id)

        if msg_id in known:
            continue

        sender = msg.get("sender_agent_id", "unknown")
        created_at = msg.get("created_at", "")
        topic = msg.get("topic") or msg.get("content_type", "message")
        payload = msg.get("payload", "")
        summary = _extract_summary(payload)

        # Full content for soul node
        content = f"""## AgentBus Message: {msg_id}

from: {sender}
to: {agent}
topic: {topic}
created_at: {created_at}
chunk_id: {chunk_id}

### Payload
{json.dumps(payload, indent=2) if isinstance(payload, dict) else str(payload)}

### Status
unread — indexed {datetime.now(timezone.utc).strftime("%Y-%m-%d")}"""

        added = _soul_add(
            content=content,
            tags=["inbox", "fleet", "message"],
            parents=[],
            node_id=msg_id,
        )

        stub = _stub_line(msg_id, sender, agent, created_at, topic, summary)
        _append_stub(stub)

        if added:
            print(f"  indexed: {stub}")
        else:
            print(f"  already in graph: {msg_id}")

        new_count += 1

    print(f"Inbox: {new_count} new messages indexed ({len(messages)} total checked).")


def cmd_reply(agent: str, msg_id: str, reply_text: str, env: dict) -> None:
    """Reply to a message: publish via AgentBus + mark soul node replied."""
    # Look up sender from soul node content
    soul_path = Path.home() / ".hermes" / "soul.json"
    sender_agent = None
    try:
        sys.path.insert(0, str(Path.home() / ".mac" / "src" / "mac" / "src"))
        from mac.soul_graph import SoulGraph  # type: ignore[import]

        g = SoulGraph.load(soul_path)
        node = g.nodes.get(msg_id)
        if node:
            for line in node.content.splitlines():
                if line.startswith("from:"):
                    sender_agent = line.split(":", 1)[1].strip()
                    break
    except Exception:
        pass

    if not sender_agent:
        print(f"[warn] Could not find sender for {msg_id}; broadcasting.")

    # Publish reply
    publish_cmd = [
        "mac",
        "admin",
        "agentbus",
        "publish",
        agent,
        "--topic",
        f"reply:{msg_id}",
        "--content-type",
        "text/plain",
        "--payload",
        reply_text,
        "--payload-encoding",
        "text",
    ]
    if sender_agent:
        publish_cmd += ["--recipient-agent-id", sender_agent]

    result = subprocess.run(publish_cmd, capture_output=True, text=True, env=env)
    if result.returncode == 0:
        print(f"Reply sent to {sender_agent or 'fleet'}.")
    else:
        print(f"[warn] Reply publish failed: {result.stderr[:100]}")

    # Mark node replied in soul graph
    soul_path = Path.home() / ".hermes" / "soul.json"
    try:
        from mac.soul_graph import SoulGraph  # type: ignore[import]

        g = SoulGraph.load(soul_path)
        node = g.nodes.get(msg_id)
        if node:
            node.content += f"\n\n### Reply sent {datetime.now(timezone.utc).strftime('%Y-%m-%d')}\n{reply_text}"
            node.tags.add("replied")
            node.tags.discard("inbox")
            g.save(soul_path)
            print(f"Soul node {msg_id} marked [replied].")
    except Exception as exc:
        print(f"[warn] Could not mark replied: {exc}")


def cmd_list(agent: str) -> None:
    """List all known inbox stubs."""
    p = _memory_stub_path()
    if not p.exists():
        print("No inbox stubs yet.")
        return
    stubs = [l for l in p.read_text().splitlines() if l.strip()]
    print(f"{len(stubs)} inbox entries:")
    for s in stubs:
        print(" ", s)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agent inbox: AgentBus → soul nodes + memory stubs"
    )
    parser.add_argument("--agent", default="agent_bullwinkle")
    parser.add_argument("--limit", type=int, default=20)
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("read", help="Read pending messages and index new ones")
    sub.add_parser("list", help="List all indexed inbox stubs")
    r = sub.add_parser("reply", help="Reply to a message by mes_id")
    r.add_argument("msg_id", help="e.g. mes_abc12345")
    r.add_argument("text", help="Reply text")

    args = parser.parse_args()
    env = _mac_env()

    if args.cmd == "read" or args.cmd is None:
        cmd_read(args.agent, args.limit, env)
    elif args.cmd == "list":
        cmd_list(args.agent)
    elif args.cmd == "reply":
        cmd_reply(args.agent, args.msg_id, args.text, env)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
