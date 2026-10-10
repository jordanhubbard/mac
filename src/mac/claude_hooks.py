"""Claude Code hooks that connect a running task agent to its task board.

The executor copies this file into the task workspace (``.mac-agent/``) and
points Claude Code's hooks at it, so it must stay standard-library only: it
runs under the sandbox image's Python, whatever MAC version built that image.

Three hooks and one tool:

``session-start``
    Explains the board to the agent and replays what people have already
    said on the task (a directive left before this run, an answer to an
    earlier question).

``post-tool``
    Runs after every tool call. Delivers anything new on the board (a
    person's directive or answer, a hub nudge or verdict) into the agent's
    context, posts the agent's current activity at most once a minute so the
    console shows it live, and, when the agent has worked a while without a
    status post, asks it for one. A hook can only add context after a tool
    call (Claude Code does not accept added context before one), which is
    soon enough: an agent makes many calls a minute.

``stop``
    Runs when the agent tries to end its turn. It refuses while there are
    unread messages for the agent, and refuses (a bounded number of times)
    an exit with no hand-off, telling the agent what is still owed: finish
    and post ``done``, say ``no-change`` with the reason, or ``ask``. This is
    what keeps an agent from stopping on "should I also ...?" when the
    answer is already in its task.

``board``
    What the agent runs to post: ``status``, ``done``, ``no-change``, ``ask``,
    ``say`` and ``read``.

A hook never fails the agent: any error is appended to ``errors.log`` beside
the state file and the hook prints nothing.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

#: Ask for a status after this many tool calls, or minutes, without one.
NUDGE_AFTER_CALLS = 25
NUDGE_AFTER_SECONDS = 10 * 60
#: Post the current tool at most this often.
ACTIVITY_INTERVAL_SECONDS = 60
#: How many times the Stop hook may refuse an exit with no hand-off.
MAX_HANDOFF_REFUSALS = 2
#: How many times it may refuse to deliver unread messages.
MAX_MESSAGE_REFUSALS = 5
HTTP_TIMEOUT_SECONDS = 8
#: The kinds that carry something for the agent (mac.task_board.FOR_AGENT_KINDS).
FOR_AGENT_KINDS = ("message", "answer", "directive", "nudge", "verdict")


# -- plumbing ----------------------------------------------------------------


def _state_dir() -> Path:
    configured = os.environ.get("MAC_AGENT_STATE_DIR", "").strip()
    return Path(configured) if configured else Path(__file__).resolve().parent / "state"


def _state_path() -> Path:
    return _state_dir() / "board-state.json"


def load_state() -> Dict[str, Any]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(state: Dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _log_error(context: str, exc: BaseException) -> None:
    try:
        path = _state_dir() / "errors.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                "%s %s: %s: %s\n"
                % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), context, type(exc).__name__, exc)
            )
    except OSError:
        pass


class Hub:
    """The task board over HTTP, with the task's inference token."""

    def __init__(self, base_url: str, token: str, task_id: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.task_id = task_id

    @classmethod
    def from_env(cls) -> Optional["Hub"]:
        base = (os.environ.get("MAC_HUB_URL") or os.environ.get("MAC_URL") or "").strip()
        token = os.environ.get("MAC_INFERENCE_TOKEN", "").strip()
        task_id = os.environ.get("MAC_TASK_ID", "").strip()
        if not (base and token and task_id):
            return None
        return cls(base, token, task_id)

    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None) -> Any:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:  # noqa: S310
            raw = response.read().decode("utf-8")
        return json.loads(raw) if raw.strip() else {}

    def read(self, after: int) -> Dict[str, Any]:
        return self._request(
            "GET",
            "/tasks/%s/messages?after=%d&limit=200" % (quote(self.task_id, safe=""), int(after)),
        )

    def post(self, kind: str, body: str, *, reply_to: Optional[int] = None, **metadata: Any) -> Any:
        payload: Dict[str, Any] = {"kind": kind, "body": body, "metadata": metadata}
        if reply_to is not None:
            payload["reply_to"] = reply_to
        return self._request("POST", "/tasks/%s/messages" % quote(self.task_id, safe=""), payload)


def _new_for_agent(hub: Hub, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Messages for the agent since the cursor; advances the cursor."""
    page = hub.read(int(state.get("cursor") or 0))
    messages = page.get("messages") or []
    if messages:
        state["cursor"] = int(messages[-1]["id"])
    return [
        m
        for m in messages
        if m.get("author_kind") != "agent" and m.get("kind") in FOR_AGENT_KINDS
    ]


def format_messages(messages: List[Dict[str, Any]]) -> str:
    lines = []
    for m in messages:
        kind = m.get("kind")
        who = m.get("author") or m.get("author_kind")
        head = "[mac board #%s] %s %s" % (m.get("id"), who, kind)
        if m.get("reply_to"):
            head += " (re #%s)" % m["reply_to"]
        lines.append("%s: %s" % (head, m.get("body")))
    note = (
        "A directive is an instruction from the person who owns this task: follow it, "
        "and if it conflicts with earlier instructions it wins. Acknowledge with "
        "`.mac-agent/board say \"...\"` when you have acted on it."
    )
    return "\n".join(lines + [note])


def _summarize_tool(payload: Dict[str, Any]) -> str:
    name = str(payload.get("tool_name") or "tool")
    tool_input = payload.get("tool_input") or {}
    detail = ""
    if isinstance(tool_input, dict):
        for key in ("command", "file_path", "path", "pattern", "url", "description"):
            if tool_input.get(key):
                detail = str(tool_input[key])
                break
    detail = " ".join(detail.split())
    if len(detail) > 160:
        detail = detail[:157] + "..."
    return "%s: %s" % (name, detail) if detail else name


def _emit_context(event: str, text: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}))


def _block(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def _now() -> float:
    return time.time()


# -- hooks -------------------------------------------------------------------


BOARD_GUIDE = """\
[mac board] This task has a board: a conversation with the person who owns the task and \
with MAC. You will see new messages from them after your tool calls, prefixed \
[mac board]. Post to it with the `.mac-agent/board` command in this workspace:

  .mac-agent/board status "what you are doing, what you found, what is next"
  .mac-agent/board ask "a question only a person can answer" [--default "what you will \
assume" --expires MINUTES] [--options "a,b"] [--blocking]
  .mac-agent/board done "what you changed, and how you checked it"
  .mac-agent/board no-change "why nothing needed to change, and how you checked"
  .mac-agent/board say "anything else"
  .mac-agent/board report "what you did" --why "..." --undo "how to reverse it"
  .mac-agent/board report "what you saw" --about-agent AGENT --about-task TASK --evidence "..."

Act, then tell. You keep your authority: do what the work needs. But whenever you \
do something a person might want to know about -- anything beyond your own task \
branch and pull request, such as changing repository settings, rulesets, branch \
protection, webhooks, secrets or collaborators; force-pushing or deleting a shared \
branch; closing someone else's PR or issue; changing a host or service; or causing \
an external side effect -- `report` it right afterwards: what, why, how to undo. \
It reaches the humans on Slack. If you notice ANOTHER agent doing something a \
person should know about, report that too, naming the agent or task and the \
evidence. Routine task work needs no report.

Work without stopping. Do not stop to ask a question you can answer from the task, \
the repository or your own judgement: decide, note the assumption in your status, \
and continue. Ask only what genuinely needs a person; give a default when one is \
reasonable, and keep working on everything the question does not block. When the \
work is finished, post `done` (or `no-change`) and then stop."""


#: What a new session is told about earlier work on the task. Old nudges and
#: activity lines are noise; direction, answers, verdicts and what earlier
#: attempts reported are not.
HISTORY_KINDS = (
    "message", "answer", "directive", "verdict", "status", "done", "question", "report"
)
MAX_HISTORY_MESSAGES = 40


def format_history(messages: List[Dict[str, Any]]) -> str:
    kept = [m for m in messages if m.get("kind") in HISTORY_KINDS][-MAX_HISTORY_MESSAGES:]
    lines = []
    for m in kept:
        who = "you (earlier)" if m.get("author_kind") == "agent" else (m.get("author") or m.get("author_kind"))
        lines.append("#%s %s %s: %s" % (m.get("id"), who, m.get("kind"), m.get("body")))
    return "\n".join(lines)


def hook_session_start(payload: Dict[str, Any], hub: Optional[Hub]) -> None:
    state = load_state()
    fresh = "cursor" not in state
    state.setdefault("started_at", _now())
    state.setdefault("last_post_at", _now())
    parts = [BOARD_GUIDE]
    if hub is not None:
        try:
            if fresh:
                # A new attempt: tell it what happened before. Earlier attempts'
                # work may be gone, but what people said and what the judge
                # found are not, and the agent should start from them.
                page = hub.read(0)
                messages = page.get("messages") or []
                if messages:
                    state["cursor"] = int(messages[-1]["id"])
                history = format_history(messages)
                if history:
                    parts.append(
                        "Earlier on this task (direction from the owner wins over the task text; "
                        "a judge's 'not_met' says what was still missing):\n" + history
                    )
            else:
                new = _new_for_agent(hub, state)
                if new:
                    parts.append(format_messages(new))
        except Exception as exc:  # noqa: BLE001
            _log_error("session-start", exc)
    save_state(state)
    _emit_context("SessionStart", "\n\n".join(parts))


def hook_post_tool(payload: Dict[str, Any], hub: Optional[Hub]) -> None:
    state = load_state()
    now = _now()
    state["calls_since_post"] = int(state.get("calls_since_post") or 0) + 1
    state.setdefault("last_post_at", now)
    parts: List[str] = []
    if hub is not None:
        try:
            if now - float(state.get("last_activity_at") or 0) >= ACTIVITY_INTERVAL_SECONDS:
                hub.post("activity", _summarize_tool(payload))
                state["last_activity_at"] = now
        except Exception as exc:  # noqa: BLE001
            _log_error("post-tool activity", exc)
        try:
            new = _new_for_agent(hub, state)
            if new:
                parts.append(format_messages(new))
        except Exception as exc:  # noqa: BLE001
            _log_error("post-tool read", exc)
    quiet_calls = int(state["calls_since_post"])
    quiet_seconds = now - float(state.get("last_post_at") or now)
    if (quiet_calls >= NUDGE_AFTER_CALLS or quiet_seconds >= NUDGE_AFTER_SECONDS) and not state.get(
        "nudged"
    ):
        state["nudged"] = True
        parts.append(
            "[mac board] status? You have made %d tool calls over %d minutes without a status. "
            'Post one line: `.mac-agent/board status "..."` (what you are doing, what you '
            "found, what is next), then continue." % (quiet_calls, int(quiet_seconds // 60))
        )
    save_state(state)
    if parts:
        _emit_context("PostToolUse", "\n\n".join(parts))


def hook_stop(payload: Dict[str, Any], hub: Optional[Hub]) -> None:
    if hub is None:
        # No board (a route probe, or a run outside a task): nothing to hand
        # off to, so nothing to hold the agent for.
        return
    state = load_state()
    if hub is not None and int(state.get("message_refusals") or 0) < MAX_MESSAGE_REFUSALS:
        try:
            new = _new_for_agent(hub, state)
        except Exception as exc:  # noqa: BLE001
            _log_error("stop read", exc)
            new = []
        if new:
            state["message_refusals"] = int(state.get("message_refusals") or 0) + 1
            save_state(state)
            _block("New messages arrived on the task board. Act on them before you stop.\n" + format_messages(new))
            return
    if state.get("handed_off") or state.get("asked_blocking"):
        save_state(state)
        return
    refusals = int(state.get("handoff_refusals") or 0)
    if refusals >= MAX_HANDOFF_REFUSALS:
        save_state(state)
        return
    state["handoff_refusals"] = refusals + 1
    save_state(state)
    _block(
        "You have not handed off this task yet, so it is not finished. Do not stop to ask "
        "whether to continue: continue. Do whichever of these is true:\n"
        "- the work is unfinished: keep working on it now;\n"
        '- it is finished: `.mac-agent/board done "what you changed and how you checked it"`, then stop;\n'
        '- nothing needed to change: `.mac-agent/board no-change "why, and how you checked"`, then stop;\n'
        '- you are blocked on something only a person can decide: `.mac-agent/board ask "..." --blocking`, '
        "then stop."
    )


# -- the agent's board command ---------------------------------------------


def board_main(argv: List[str], hub: Optional[Hub]) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog=".mac-agent/board", description="Post to this task's board.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("status", "say", "done", "no-change"):
        p = sub.add_parser(name)
        p.add_argument("text")
    ask = sub.add_parser("ask")
    ask.add_argument("text")
    ask.add_argument("--default", default=None, help="what you will assume if nobody answers")
    ask.add_argument("--options", default=None, help="comma-separated choices")
    ask.add_argument(
        "--expires",
        type=int,
        default=None,
        metavar="MINUTES",
        help="with --default: apply the default if nobody answers within this many minutes",
    )
    ask.add_argument(
        "--blocking",
        action="store_true",
        help="nothing else can proceed until this is answered (you will stop after asking)",
    )
    report = sub.add_parser("report", help="tell the humans what you did, or saw another agent do")
    report.add_argument("text")
    report.add_argument("--why", default=None)
    report.add_argument("--undo", default=None)
    report.add_argument("--about-agent", default=None)
    report.add_argument("--about-task", default=None)
    report.add_argument("--evidence", default=None)
    report.add_argument("--key", default=None)
    sub.add_parser("read")
    args = parser.parse_args(argv)
    if hub is None:
        print("board unavailable: no hub URL, token or task id in the environment", file=sys.stderr)
        return 2
    state = load_state()
    try:
        if args.command == "read":
            messages = hub.read(0).get("messages") or []
            for m in messages:
                print("#%s %s %s: %s" % (m["id"], m.get("author"), m.get("kind"), m.get("body")))
            return 0
        if args.command == "ask":
            metadata: Dict[str, Any] = {"blocking": bool(args.blocking)}
            if args.default is not None:
                metadata["default"] = args.default
            if args.options:
                metadata["options"] = [o.strip() for o in args.options.split(",") if o.strip()]
            if args.expires:
                metadata["expires_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(_now() + 60 * int(args.expires))
                )
            posted = hub.post("question", args.text, **metadata)
            if args.blocking:
                state["asked_blocking"] = True
            print("asked as #%s; keep working on anything it does not block" % posted.get("id"))
        elif args.command == "done":
            hub.post("done", args.text)
            state["handed_off"] = True
            print("handed off; you may stop")
        elif args.command == "report":
            fields = {
                "report": "peer" if (args.about_agent or args.about_task) else "self",
                "why": args.why,
                "undo": args.undo,
                "about_agent": args.about_agent,
                "about_task": args.about_task,
                "evidence": args.evidence,
                "key": args.key,
            }
            posted = hub.post("report", args.text, **{k: v for k, v in fields.items() if v})
            print("reported as #%s; the humans will see it on Slack" % posted.get("id"))
        elif args.command == "no-change":
            hub.post("done", args.text, no_change=True)
            state["handed_off"] = True
            print("recorded no change needed; you may stop")
        else:
            hub.post("status" if args.command == "status" else "message", args.text)
            print("posted")
    except urllib.error.HTTPError as exc:
        print("board refused the post: HTTP %s %s" % (exc.code, exc.read()[:300]), file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001
        print("board unavailable: %s" % exc, file=sys.stderr)
        return 1
    state["calls_since_post"] = 0
    state["last_post_at"] = _now()
    state["nudged"] = False
    save_state(state)
    return 0


HOOKS = {"session-start": hook_session_start, "post-tool": hook_post_tool, "stop": hook_stop}


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: claude_hooks.py session-start|post-tool|stop|board ...", file=sys.stderr)
        return 2
    command, rest = argv[0], argv[1:]
    hub = Hub.from_env()
    if command == "board":
        return board_main(rest, hub)
    handler = HOOKS.get(command)
    if handler is None:
        return 2
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        handler(payload if isinstance(payload, dict) else {}, hub)
    except Exception as exc:  # noqa: BLE001 - a hook must never fail the agent
        _log_error(command, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
