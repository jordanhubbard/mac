"""The hub's Slack relay: agent reports and questions out, people's answers back.

The notifier does not post to Slack itself. For ``agent.report`` ("act, then
tell") and ``task.question`` it queues a ``status_update`` message in the
mailbox of the agent its Slack channel targets, and something on the hub has to
drain that mailbox and post through the Hermes Slack accounts. That used to be
the hub's own task agent (``com.mac.agent``). When rocky became hub-and-chat
only (#1006, #1007) that agent went away and the relay went with it: reports
and questions sat ``queued`` and nobody was told.

This runs the same relay (``mac.worker.SlackChatRelayMixin``) without a task
loop, so the hub can carry it and still execute no tasks:

* ``run`` drains the mailbox of every enabled Slack notifier target, posts each
  notification to the Hermes home channels, and relays thread replies (or
  ``Q7 answer`` messages) back to the hub as answers. It authenticates with the
  hub's admin token, which the question endpoints accept for any notification.
* ``install`` makes the hub match: ``~/.mac/bin/slack-relay`` plus the
  LaunchAgent ``com.mac.slack-relay`` (kept alive), restarted so a fleet update
  runs the new code. ``scripts/fleet-update`` runs it on the hub.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import plistlib
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

from mac.worker import JsonDict, SlackChatRelayMixin

LABEL = "com.mac.slack-relay"
DEFAULT_HUB_URL = "http://127.0.0.1:8789"
#: The hub's former task agent kept its question state here; the relay starts
#: from it so question codes keep counting up instead of restarting at Q1.
LEGACY_STATE = Path("agent-workspaces") / ".mac-agentbus-control-chat-questions.json"
#: Notification kinds the relay forwards. Task progress has its own channel.
RELAYED_EVENTS = frozenset({"agent.report", "task.question"})
TARGET_REFRESH_SECONDS = 300.0

LAUNCHER = """#!/bin/bash
# Managed by mac.slack_relay; replaced on every fleet update.
set -a; [ -f "{home}/mac.env" ] && . "{home}/mac.env"; set +a
exec "{home}/venv/bin/python" -m mac.slack_relay run
"""

Runner = Callable[[Sequence[str]], "tuple[int, str]"]


def _log(event: str, level: str = "info", **detail: Any) -> None:
    line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, "level": level}
    line.update(detail)
    print(json.dumps(line, sort_keys=True, default=str), file=sys.stderr, flush=True)


class AgentMailboxRelay(SlackChatRelayMixin):
    """The relay for one notifier target's mailbox. Executes no tasks."""

    def __init__(self, client: Any, agent_id: str, state_path: Path, poll_seconds: float) -> None:
        self.client = client
        self.agent_id = agent_id
        self.chat_questions_path = state_path
        self.chat_poll_seconds = poll_seconds
        self.status_update_sink = self._send_status_update_to_home_channels
        self._chat_lock = threading.Lock()
        self._chat_thread: Optional[threading.Thread] = None
        self._slack_user_names: Dict[str, str] = {}
        self._stop = False

    def _observe_log(
        self,
        name: str,
        level: str = "info",
        subject_type: Optional[str] = None,
        subject_id: Optional[str] = None,
        detail: Optional[JsonDict] = None,
    ) -> None:
        _log(name.replace("worker.", "slack_relay.", 1), level, agent_id=self.agent_id,
             subject_type=subject_type, subject_id=subject_id, detail=detail or {})

    def poll_once(self) -> None:
        self._process_control_messages()
        self._relay_chat_question_replies()


def notifier_targets(client: Any) -> List[str]:
    """Agent ids whose mailboxes enabled Slack notifiers queue reports/questions in."""
    channels = client.get("/notifier/channels?enabled=true&channel_type=slack")
    targets: List[str] = []
    for channel in channels if isinstance(channels, list) else []:
        if not isinstance(channel, dict) or not channel.get("enabled", True):
            continue
        if not RELAYED_EVENTS & {str(e) for e in channel.get("event_types") or []}:
            continue
        agent_id = str((channel.get("target") or {}).get("agent_id") or "").strip()
        if agent_id and agent_id not in targets:
            targets.append(agent_id)
    return targets


def state_path_for(mac_home: Path, agent_id: str) -> Path:
    path = mac_home / "slack-relay" / ("%s-chat-questions.json" % agent_id)
    legacy = mac_home / LEGACY_STATE
    if not path.exists() and legacy.is_file():
        # One host ran one agent, so its old state belongs to the agent it relayed for.
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
        _log("slack_relay.state_seeded", agent_id=agent_id, source=str(legacy))
    return path


def run(*, client: Any, mac_home: Path, poll_seconds: float, once: bool = False) -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    relays: Dict[str, AgentMailboxRelay] = {}
    refreshed = 0.0
    while not stop.is_set():
        if time.monotonic() - refreshed > TARGET_REFRESH_SECONDS or not relays:
            try:
                targets = notifier_targets(client)
            except Exception as exc:  # noqa: BLE001 - a hub restart must not end the relay
                _log("slack_relay.targets_failed", "warning", error=str(exc))
                targets = list(relays)
            else:
                refreshed = time.monotonic()
                if sorted(targets) != sorted(relays):
                    _log("slack_relay.targets", targets=targets)
            relays = {
                agent_id: relays.get(agent_id)
                or AgentMailboxRelay(client, agent_id, state_path_for(mac_home, agent_id), poll_seconds)
                for agent_id in targets
            }
        for relay in relays.values():
            try:
                relay.poll_once()
            except Exception as exc:  # noqa: BLE001 - one bad poll must not end the relay
                _log("slack_relay.poll_failed", "warning", agent_id=relay.agent_id, error=str(exc))
        if once:
            return 0
        stop.wait(poll_seconds)
    return 0


# --- install (hub, macOS) ----------------------------------------------------


def _run(cmd: Sequence[str]) -> "tuple[int, str]":
    proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=60)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def _write_if_changed(path: Path, text: str, mode: int) -> bool:
    if path.is_file() and path.read_text(encoding="utf-8", errors="replace") == text:
        os.chmod(path, mode)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)
    return True


def render_plist(launcher: Path, log: Path) -> str:
    return plistlib.dumps(
        {
            "Label": LABEL,
            "ProgramArguments": [str(launcher)],
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 30,
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }
    ).decode("utf-8")


def install(
    *,
    mac_home: Path,
    agents_dir: Optional[Path] = None,
    system: Optional[str] = None,
    runner: Runner = _run,
) -> Dict[str, str]:
    if (system or platform.system()) != "Darwin":
        return {"status": "skipped", "reason": "the hub relay is a macOS LaunchAgent"}
    agents_dir = agents_dir or Path.home() / "Library" / "LaunchAgents"
    launcher = mac_home / "bin" / "slack-relay"
    plist = agents_dir / (LABEL + ".plist")
    changed = [
        name
        for name, path, text, mode in (
            ("launcher", launcher, LAUNCHER.format(home=mac_home), 0o755),
            ("plist", plist, render_plist(launcher, mac_home / "logs" / "slack-relay.log"), 0o644),
        )
        if _write_if_changed(path, text, mode)
    ]
    service = "gui/%d/%s" % (os.getuid(), LABEL)
    loaded = runner(["launchctl", "print", service])[0] == 0
    if loaded and "plist" in changed:
        runner(["launchctl", "bootout", service])
        loaded = False
    if not loaded:
        code, out = runner(["launchctl", "bootstrap", "gui/%d" % os.getuid(), str(plist)])
        if code:
            return {"status": "error", "error": "loading %s: %s" % (LABEL, out[-300:])}
    else:
        # A fleet update changed the code under a running relay: restart it.
        code, out = runner(["launchctl", "kickstart", "-k", service])
        if code:
            return {"status": "error", "error": "restarting %s: %s" % (LABEL, out[-300:])}
    return {"status": "ok", "units": "updated: %s" % ", ".join(changed) if changed else "unchanged"}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run_cmd = sub.add_parser("run", help="relay until stopped")
    run_cmd.add_argument("--once", action="store_true", help="one pass, then exit")
    sub.add_parser("install", help="install or restart the hub LaunchAgent")
    args = parser.parse_args(argv)

    from mac import mac_paths

    mac_home = mac_paths.mac_home()
    if args.command == "install":
        try:
            result = install(mac_home=mac_home)
        except Exception as exc:  # noqa: BLE001 - reported, never half-silent
            result = {"status": "error", "error": "%s: %s" % (type(exc).__name__, exc)}
        print(json.dumps(result, sort_keys=True))
        return 0 if result.get("status") != "error" else 1

    from mac.api_client import MacApiClient

    token = os.environ.get("MAC_API_TOKEN", "").strip()
    if not token:
        _log("slack_relay.no_token", "error", error="MAC_API_TOKEN is not set")
        return 2
    url = (os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_API_URL") or DEFAULT_HUB_URL).strip()
    try:
        poll = max(5.0, float(os.environ.get("MAC_SLACK_RELAY_POLL_SECONDS") or 15.0))
    except ValueError:
        poll = 15.0
    _log("slack_relay.started", hub=url, poll_seconds=poll)
    return run(client=MacApiClient(url, token=token), mac_home=mac_home, poll_seconds=poll, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
