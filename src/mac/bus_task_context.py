"""What the fleet said that this task needs to know BEFORE it starts.

The broadcast channel (``mac.agentbus_broadcast``) has been write-only in
practice: workers announce ``git.pushed`` / ``git.worktree_added`` and nothing
reads them back. The cost is measurable — eight duplicate pull requests (#405,
#437, #442, #443, #445-448) were opened against work that had already merged,
because no agent could find out that its own task was finished.

This module is the READ side, and it is deliberately a pure function over an
already-fetched feed:

* the worker owns the transport (it has the hub client and the cursor);
* the finalizer owns the decision not to open a second pull request;
* the executor prompt owns the rendering;

...and all three agree on relevance and bounds because all three call in here.

Two properties are load-bearing.

**Relevance is explicit.** An agent handed "the last N events on the bus"
learns to ignore them. What it is handed instead is the subset that names its
own task, its own project, its own repository, or a tip its work is built on —
and every entry carries WHY it was selected, so a reader can judge it.

**The bound is visible.** Truncation is announced (``truncated``,
``omitted``), never silent, for the same reason
``BroadcastService._bounded_payload`` announces it: a consumer that cannot
tell a complete context from a clipped one will eventually trust a clipped
one.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Iterable, List, Optional, Sequence

BUS_TASK_CONTEXT_SCHEMA = "mac.bus_task_context.v1"

#: How many events reach the coding agent.
#:
#: Measured event size on the live hub is 531-589 bytes, ~140 tokens each, so
#: 50 events is ~7k tokens. A task run spends ~900k, which makes this about
#: 0.8% of the budget to answer "has my work already landed, and has the trunk
#: moved under me" — questions whose wrong answers cost a whole duplicate run.
#: Above ~50 the marginal event is old enough that the ledger is the better
#: source anyway.
BUS_TASK_CONTEXT_EVENT_BOUND = 50

#: Terminal events that mean THIS task's work is already in the trunk.
TASK_TERMINAL_EVENT_TYPES = ("git.merged",)

#: Events that mean the trunk moved.
CANONICAL_MOVE_EVENT_TYPES = ("git.canonical_advanced", "git.merged")

#: Events that mean a peer is holding something in this repository.
PEER_HOLD_EVENT_TYPES = (
    "task.claimed",
    "git.worktree_added",
    "git.branch_created",
    "git.pushed",
    "git.merge_conflict",
)


def context_event_bound(environ: Optional[Dict[str, str]] = None) -> int:
    """The bound, overridable per deployment but never unbounded."""
    env = environ if environ is not None else os.environ
    raw = str(env.get("MAC_BUS_TASK_CONTEXT_EVENTS", "") or "").strip()
    try:
        value = int(raw) if raw else BUS_TASK_CONTEXT_EVENT_BOUND
    except ValueError:
        value = BUS_TASK_CONTEXT_EVENT_BOUND
    return max(1, min(value, 200))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _payload(event: Any) -> Dict[str, Any]:
    body = event.get("payload") if isinstance(event, dict) else None
    return body if isinstance(body, dict) else {}


def _shas(payload: Dict[str, Any]) -> List[str]:
    return [
        _text(payload.get(key))
        for key in ("sha", "from_sha", "to_sha", "base_sha", "head_sha", "tree_sha")
        if _text(payload.get(key))
    ]


def _branches(payload: Dict[str, Any]) -> List[str]:
    return [
        _text(payload.get(key)) for key in ("branch", "canonical_branch") if _text(payload.get(key))
    ]


def task_focus(
    task: Optional[Dict[str, Any]],
    repository_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, str]:
    """What "relevant to this task" means, derived from the task itself.

    Everything here comes from the task record and the prepared worktree — no
    guessing, and no dependence on the bus having said something first.
    """
    task = task if isinstance(task, dict) else {}
    repo = repository_context if isinstance(repository_context, dict) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    runtime = metadata.get("runtime") if isinstance(metadata.get("runtime"), dict) else {}
    if not repo:
        repo = runtime
    origin = metadata.get("origin") if isinstance(metadata.get("origin"), dict) else {}
    contract = (
        metadata.get("repository_contract")
        if isinstance(metadata.get("repository_contract"), dict)
        else origin.get("repository_contract")
        if isinstance(origin.get("repository_contract"), dict)
        else {}
    )
    return {
        "task_id": _text(task.get("id")),
        "project": _text(task.get("project")),
        "repository": (
            _text(repo.get("repository_canonical_remote"))
            or _text(repo.get("repository_origin_remote"))
            or _text(contract.get("canonical_remote"))
            or _text(origin.get("repository_path"))
        ),
        "branch": _text(repo.get("repository_branch")),
        "canonical_branch": (
            _text(repo.get("repository_canonical_branch"))
            or _text(contract.get("canonical_branch"))
        ),
        "base_sha": _text(repo.get("repository_base_sha")),
    }


def relevance(event: Dict[str, Any], focus: Dict[str, str]) -> str:
    """Why this event matters to this task — or "" when it does not.

    The minimum relevance contract: same project, same repository, same task,
    or an event naming a branch/tip the task builds on.
    """
    if not isinstance(event, dict):
        return ""
    if event.get("self_emitted"):
        # An agent reasoning about its own echo learns nothing and risks
        # concluding a peer is doing what it is itself doing.
        return ""
    payload = _payload(event)
    task_id = _text(focus.get("task_id"))
    if task_id and _text(event.get("task_id")) == task_id:
        return "same task"
    repository = _text(focus.get("repository"))
    if repository and _text(payload.get("repository")) == repository:
        return "same repository"
    branches = {b for b in (focus.get("branch"), focus.get("canonical_branch")) if b}
    if branches & set(_branches(payload)):
        return "names a branch this task builds on"
    base_sha = _text(focus.get("base_sha"))
    if base_sha and base_sha in _shas(payload):
        return "names the tip this task was cut from"
    project = _text(focus.get("project"))
    if project and _text(event.get("project")) == project:
        return "same project"
    return ""


def _entry(event: Dict[str, Any], why: str) -> Dict[str, Any]:
    return {
        "sequence": int(event.get("sequence") or 0),
        "created_at": event.get("created_at"),
        "event_type": _text(event.get("event_type")),
        "agent_id": _text(event.get("agent_id")),
        "task_id": _text(event.get("task_id")),
        "project": _text(event.get("project")),
        "payload": _payload(event),
        "relevance": why,
    }


def build_bus_task_context(
    events: Iterable[Dict[str, Any]],
    focus: Dict[str, str],
    *,
    bound: Optional[int] = None,
) -> Dict[str, Any]:
    """Turn a drained feed into the context this task starts with."""
    limit = int(bound if bound is not None else context_event_bound())
    selected: List[Dict[str, Any]] = []
    scanned = 0
    for event in events or []:
        scanned += 1
        why = relevance(event, focus)
        if why:
            selected.append(_entry(event, why))
    selected.sort(key=lambda item: int(item.get("sequence") or 0), reverse=True)
    kept = selected[:limit]
    omitted = len(selected) - len(kept)
    context: Dict[str, Any] = {
        "schema": BUS_TASK_CONTEXT_SCHEMA,
        "focus": dict(focus),
        "bound": limit,
        "scanned": scanned,
        "relevant": len(selected),
        "included": len(kept),
        "omitted": omitted,
        # Announced, not silent: a reader that cannot tell a complete context
        # from a clipped one will eventually trust a clipped one.
        "truncated": bool(omitted),
        "events": kept,
    }
    context["signals"] = derive_signals(kept, focus)
    return context


def derive_signals(entries: Sequence[Dict[str, Any]], focus: Dict[str, str]) -> Dict[str, Any]:
    """The three questions this exists to answer, answered.

    Derived from the BOUNDED entries, not the full scan, so what the coding
    agent is told is exactly what it can see and check.
    """
    task_id = _text(focus.get("task_id"))
    canonical = _text(focus.get("canonical_branch"))
    base_sha = _text(focus.get("base_sha"))

    already_published: Optional[Dict[str, Any]] = None
    canonical_advanced: Optional[Dict[str, Any]] = None
    peers: List[Dict[str, Any]] = []
    for entry in entries:
        event_type = _text(entry.get("event_type"))
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        if (
            already_published is None
            and event_type in TASK_TERMINAL_EVENT_TYPES
            and task_id
            and _text(entry.get("task_id")) == task_id
        ):
            already_published = {
                "event_type": event_type,
                "sequence": entry.get("sequence"),
                # Tree identity, not commit identity: the merge is a squash, so
                # the commit sha below is newly minted and matches nothing this
                # task ever saw. The tree is what survives the squash.
                "tree_sha": _text(payload.get("tree_sha")),
                "sha": _text(payload.get("sha")),
                "pr_number": payload.get("pr_number"),
                "url": _text(payload.get("url")),
                "branch": _text(payload.get("branch")),
            }
        if (
            canonical_advanced is None
            and event_type in CANONICAL_MOVE_EVENT_TYPES
            and canonical
            and _text(payload.get("canonical_branch")) == canonical
        ):
            to_sha = _text(payload.get("to_sha")) or _text(payload.get("sha"))
            if to_sha and to_sha != base_sha:
                canonical_advanced = {
                    "canonical_branch": canonical,
                    "from_sha": _text(payload.get("from_sha")),
                    "to_sha": to_sha,
                    "tree_sha": _text(payload.get("tree_sha")),
                    "sequence": entry.get("sequence"),
                    "base_sha_at_prepare": base_sha,
                }
        if event_type in PEER_HOLD_EVENT_TYPES and _text(entry.get("agent_id")):
            peers.append(
                {
                    "agent_id": _text(entry.get("agent_id")),
                    "event_type": event_type,
                    "task_id": _text(entry.get("task_id")),
                    "branch": _text(payload.get("branch")),
                    "worktree": _text(payload.get("worktree")),
                }
            )
    return {
        "already_published": already_published,
        "canonical_advanced": canonical_advanced,
        "peer_activity": peers[:10],
    }


def bus_context_from_task(task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The context the worker attached to the task, as the executor sees it."""
    task = task if isinstance(task, dict) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    runtime = metadata.get("runtime") if isinstance(metadata.get("runtime"), dict) else {}
    context = runtime.get("bus_context")
    return context if isinstance(context, dict) else {}


def already_published(context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The terminal event saying this task's work is already in the trunk."""
    if not isinstance(context, dict):
        return None
    signals = context.get("signals")
    if not isinstance(signals, dict):
        return None
    landed = signals.get("already_published")
    return landed if isinstance(landed, dict) else None


def canonical_moved(context: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The event saying the trunk moved under this task since it was prepared."""
    if not isinstance(context, dict):
        return None
    signals = context.get("signals")
    if not isinstance(signals, dict):
        return None
    moved = signals.get("canonical_advanced")
    return moved if isinstance(moved, dict) else None


def render_bus_context_section(context: Optional[Dict[str, Any]]) -> str:
    """The prompt section. Returns "" when there is nothing worth saying.

    Written as facts plus the two conclusions an agent must not have to infer:
    the work may already be landed, and the base may have moved.
    """
    if not isinstance(context, dict) or not context.get("events"):
        return ""
    lines = [
        "AgentBus context (read this before you start; it is what the rest of "
        "the fleet did while this task was queued):",
    ]
    signals = context.get("signals") if isinstance(context.get("signals"), dict) else {}
    landed = signals.get("already_published")
    if isinstance(landed, dict):
        lines.append(
            "- ALREADY LANDED: this task's work was merged (pr #%s, tree %s, %s). "
            "Verify against the repository before doing anything: do NOT re-do "
            "the change and do NOT open a second pull request. If the tree "
            "already contains the change, say so in the evidence and stop."
            % (
                landed.get("pr_number"),
                (_text(landed.get("tree_sha")) or "unknown")[:12],
                _text(landed.get("url")) or "no url",
            )
        )
    moved = signals.get("canonical_advanced")
    if isinstance(moved, dict):
        lines.append(
            "- BASE MOVED: %s advanced to %s since this worktree was cut from "
            "%s. Rebase onto the current tip before you push, and expect "
            "conflicts in files that moved."
            % (
                moved.get("canonical_branch"),
                (_text(moved.get("to_sha")) or "unknown")[:12],
                (_text(moved.get("base_sha_at_prepare")) or "unknown")[:12],
            )
        )
    peers = signals.get("peer_activity")
    if isinstance(peers, list) and peers:
        held = ", ".join(
            "%s on %s" % (item.get("agent_id"), item.get("branch") or item.get("task_id") or "?")
            for item in peers[:5]
        )
        lines.append(
            "- PEERS ACTIVE in this repository: %s. If one of them says they own "
            "a file you were about to change, believe them." % held
        )
    lines.append("- Events (newest first):")
    for entry in context["events"]:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else {}
        facts = " ".join(
            "%s=%s" % (key, payload[key])
            for key in ("branch", "canonical_branch", "sha", "tree_sha", "pr_number", "worktree")
            if payload.get(key)
        )
        lines.append(
            "    [%s] %s by %s (%s)%s"
            % (
                entry.get("sequence"),
                entry.get("event_type"),
                entry.get("agent_id") or "?",
                entry.get("relevance"),
                (" " + facts) if facts else "",
            )
        )
    if context.get("truncated"):
        lines.append(
            "- TRUNCATED: %s further relevant events were omitted to stay inside "
            "the %s-event bound. This context is incomplete; if the answer "
            "matters, query the hub (`mac admin agentbus broadcast read`) rather "
            "than assuming these are all of them." % (context.get("omitted"), context.get("bound"))
        )
    return "\n".join(lines)
