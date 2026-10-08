"""Keep Hermes out of answers to MAC task questions in Slack.

Managed by mac.hermes_question_gate; reinstalled by every fleet update.

MAC posts a task question to the Slack home channel as "*Q7* Answer needed: ...",
and a person answers by replying in that thread or by posting "Q7 <answer>" in
the channel. The MAC worker reads those answers straight from Slack and records
them on the task board. Hermes answers everything in a free-response channel,
so without this gate every agent replies to an answer meant for MAC.

This plugin drops, before the agent sees them:

- a top-level Slack message that starts with a question code ("Q7 blue");
- a reply in a thread whose parent is a MAC question.

A message that @-mentions someone still goes through, so a person can pull an
agent into a question thread. Nothing else is touched. The plugin is
self-contained (it runs in Hermes' interpreter, not MAC's).
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Same shape the MAC worker accepts (mac.worker._QUESTION_CODE_ANSWER).
_CODE_ANSWER = re.compile(r"^\s*[*_`]*Q(\d+)[*_`]*(?:\s*[:,.\-–—]\s*|\s+)\S", re.I)
# The question post: "*Q7* Answer needed: ...", as Slack thread context renders it.
_QUESTION_PARENT = re.compile(r"\bQ\d+\b\W{0,4}Answer needed\b", re.I)
_THREAD_PARENT = "[thread parent]"
_MENTION = re.compile(r"<@[UW][A-Z0-9]+")

SKIP_CODE = {"action": "skip", "reason": "mac-question-gate: coded answer to a MAC task question"}
SKIP_THREAD = {"action": "skip", "reason": "mac-question-gate: reply in a MAC task question thread"}


def _platform(event: Any) -> str:
    platform = getattr(getattr(event, "source", None), "platform", None)
    return str(getattr(platform, "value", platform) or "").lower()


def decide(event: Any) -> Optional[Dict[str, str]]:
    """The hook result for one incoming message: a skip, or None to let it through."""
    if event is None or _platform(event) != "slack":
        return None
    raw = getattr(event, "raw_message", None)
    raw = raw if isinstance(raw, dict) else {}
    text = str(getattr(event, "text", "") or "")
    if _MENTION.search(str(raw.get("text") or text)):
        return None  # addressed to someone: their call, not ours
    ts = str(raw.get("ts") or "")
    thread_ts = str(raw.get("thread_ts") or "")
    in_thread = bool(getattr(event, "reply_to_message_id", None)) or bool(thread_ts and thread_ts != ts)
    if in_thread:
        # A first reply in a thread arrives with the thread's messages as context,
        # parent first. The worker posts questions over the Web API, so no agent
        # has a session in a question thread and the parent is always there.
        context = str(getattr(event, "channel_context", "") or "")
        for line in context.splitlines():
            if line.startswith(_THREAD_PARENT) and _QUESTION_PARENT.search(line):
                return SKIP_THREAD
        return None
    if _CODE_ANSWER.match(text):
        return SKIP_CODE
    return None


def _pre_gateway_dispatch(event: Any = None, **_: Any) -> Optional[Dict[str, str]]:
    try:
        return decide(event)
    except Exception:  # noqa: BLE001 - never block a message on our own bug
        return None


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _pre_gateway_dispatch)
