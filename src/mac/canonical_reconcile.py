"""Force a look at canonical HEAD before assuming a repo_change is still required.

Workspace prep already places the agent on the current canonical tip. That is
placement, not a validity decision. AgentBus ``already_published`` is keyed to
THIS task's merge (the duplicate-PR failure). Adjacent landings on the same
files are not proof the described defect is gone — nanolang #107/#108 is the
counterexample: a release PR touched ``scripts/release.sh`` and the direct-push
bug was still on HEAD.

This module is the recorded look:

* extract likely paths from the task title/description
* list recent commits that touched them (or the branch, if no paths)
* render those facts into the executor prompt
* validate that repository evidence carries a decision matching the evidence type

Auto-cancel is intentionally absent. The host refuses a repo_change that never
did this reconcile; it does not guess that the work is obsolete.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

CANONICAL_RECONCILE_SCHEMA = "mac.canonical_reconcile.v1"
RECONCILE_DECISIONS = frozenset({"still_valid", "already_satisfied", "needs_restatement"})
NO_CHANGE_DECISIONS = frozenset({"already_satisfied", "needs_restatement"})
REPO_CHANGE_DECISIONS = frozenset({"still_valid"})
RECENT_COMMIT_LIMIT = 15

_BACKTICK_PATH = re.compile(r"`([^`]+)`")
_SLASH_PATH = re.compile(r"(?<![\w./-])((?:[A-Za-z0-9_.-]+/){1,}[A-Za-z0-9_.-]+)")
_GIT_SHA = re.compile(r"^[0-9a-fA-F]{7,64}$")
_SKIP_PATH_PREFIXES = (
    "http://",
    "https://",
    "git@",
    "ssh://",
    "git://",
    "file://",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def implicated_paths(title: str, description: str) -> List[str]:
    """Path-like tokens the task statement is likely talking about."""

    blob = "%s\n%s" % (title or "", description or "")
    found: List[str] = []
    seen = set()
    for match in _BACKTICK_PATH.finditer(blob):
        _remember_path(found, seen, match.group(1), from_backtick=True)
    for match in _SLASH_PATH.finditer(blob):
        _remember_path(found, seen, match.group(1), from_backtick=False)
    return found


def _remember_path(
    found: List[str],
    seen: set[str],
    raw: str,
    *,
    from_backtick: bool,
) -> None:
    path = _text(raw).strip(".,;:()[]{}<>\"'")
    if not path or path in seen:
        return
    lowered = path.lower()
    if any(lowered.startswith(prefix) for prefix in _SKIP_PATH_PREFIXES):
        return
    if "://" in path or path.startswith("git@"):
        return
    if "/" not in path:
        # Bare filenames only from backticks (`release.sh`, `Makefile`).
        if not from_backtick or "." not in path:
            return
    seen.add(path)
    found.append(path)


def expected_head_sha_from_task(task: Optional[Mapping[str, Any]]) -> str:
    snapshot = reconcile_snapshot_from_task(task)
    return _text(snapshot.get("head_sha"))


def reconcile_snapshot_from_task(task: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    task = task if isinstance(task, Mapping) else {}
    metadata = task.get("metadata") if isinstance(task.get("metadata"), Mapping) else {}
    runtime = metadata.get("runtime") if isinstance(metadata.get("runtime"), Mapping) else {}
    snapshot = runtime.get("canonical_reconcile")
    return dict(snapshot) if isinstance(snapshot, Mapping) else {}


def _git(
    worktree: Path, args: Sequence[str], *, timeout: float = 15.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )


def recent_commits_touching(
    worktree: Optional[Path],
    paths: Sequence[str],
    *,
    limit: int = RECENT_COMMIT_LIMIT,
) -> List[Dict[str, Any]]:
    """Recent commits on the prepared HEAD, optionally restricted to *paths*."""

    if worktree is None or not Path(worktree).is_dir():
        return []
    root = Path(worktree)
    argv = [
        "log",
        "-n",
        str(max(1, min(int(limit), 50))),
        "--format=%H\t%s",
        "--",
    ]
    existing = [path for path in paths if path]
    if existing:
        argv.extend(existing)
    try:
        proc = _git(root, argv)
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    commits: List[Dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        sha, sep, subject = line.partition("\t")
        sha = sha.strip()
        if not _GIT_SHA.match(sha):
            continue
        commits.append(
            {
                "sha": sha,
                "subject": subject.strip(),
                "paths": _commit_paths(root, sha, existing),
            }
        )
    return commits


def _commit_paths(worktree: Path, sha: str, implicated: Sequence[str]) -> List[str]:
    try:
        proc = _git(worktree, ["diff-tree", "--no-commit-id", "--name-only", "-r", sha])
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    names = [_text(line) for line in proc.stdout.splitlines() if _text(line)]
    if not implicated:
        return names[:20]
    implicated_set = set(implicated)
    return [name for name in names if name in implicated_set] or names[:20]


def sibling_landings_from_bus(
    context: Optional[Mapping[str, Any]],
    *,
    task_id: str,
) -> List[Dict[str, Any]]:
    """Same-repo merges/pushes that are not THIS task's landing.

    Facts for the prompt. Never treated as ``already_published``.
    """

    if not isinstance(context, Mapping):
        return []
    events = context.get("events")
    if not isinstance(events, list):
        return []
    mine = _text(task_id)
    landings: List[Dict[str, Any]] = []
    seen = set()
    for entry in events:
        if not isinstance(entry, Mapping):
            continue
        event_type = _text(entry.get("event_type"))
        if event_type not in {"git.merged", "git.pushed", "git.pr_opened"}:
            continue
        if mine and _text(entry.get("task_id")) == mine:
            continue
        payload = entry.get("payload") if isinstance(entry.get("payload"), Mapping) else {}
        key = (
            event_type,
            _text(entry.get("task_id")),
            _text(payload.get("pr_number") or payload.get("sha") or payload.get("branch")),
        )
        if key in seen:
            continue
        seen.add(key)
        paths = _payload_paths(payload)
        landings.append(
            {
                "event_type": event_type,
                "task_id": _text(entry.get("task_id")),
                "agent_id": _text(entry.get("agent_id")),
                "pr_number": payload.get("pr_number"),
                "sha": _text(
                    payload.get("sha") or payload.get("head_sha") or payload.get("tree_sha")
                ),
                "branch": _text(payload.get("branch") or payload.get("canonical_branch")),
                "paths": paths,
                "relevance": _text(entry.get("relevance")),
            }
        )
        if len(landings) >= 10:
            break
    return landings


def _payload_paths(payload: Mapping[str, Any]) -> List[str]:
    for key in ("paths", "files", "files_changed"):
        value = payload.get(key)
        if isinstance(value, list):
            return [_text(item) for item in value if _text(item)][:20]
    return []


def build_reconcile_snapshot(
    worktree: Optional[Path],
    task: Mapping[str, Any],
    *,
    head_sha: str,
    canonical_branch: str,
    bus_context: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    title = _text(task.get("title"))
    description = _text(task.get("description"))
    paths = implicated_paths(title, description)
    return {
        "schema": CANONICAL_RECONCILE_SCHEMA,
        "head_sha": _text(head_sha),
        "canonical_branch": _text(canonical_branch) or "main",
        "implicated_paths": paths,
        "recent_commits": recent_commits_touching(worktree, paths),
        "sibling_landings": sibling_landings_from_bus(bus_context, task_id=_text(task.get("id"))),
    }


def render_reconcile_section(task: Optional[Mapping[str, Any]]) -> str:
    """Prompt facts. Empty unless workspace prep attached a snapshot."""

    snapshot = reconcile_snapshot_from_task(task)
    if not snapshot:
        return ""
    head = _text(snapshot.get("head_sha")) or "unknown"
    branch = _text(snapshot.get("canonical_branch")) or "main"
    lines = [
        "Canonical HEAD reconcile (required before you edit):",
        "- You are on %s at %s. Starting on HEAD is placement, not proof the "
        "task is still valid." % (branch, head[:12] if head != "unknown" else head),
        "- Confirm the requested change is still absent in THIS tree. Adjacent "
        "landings on the same files are not proof the defect is gone.",
        "- Record canonical_reconcile in mac-evidence.json with decision "
        "still_valid | already_satisfied | needs_restatement, this HEAD sha, "
        "and a reason.",
        "- still_valid: implement (evidence_type=repo_change).",
        "- already_satisfied: do not re-implement; evidence_type=no_change; "
        "cite this HEAD. Do not open a pull request.",
        "- needs_restatement: the statement no longer matches the tree; "
        "evidence_type=no_change; do not invent a different change.",
    ]
    paths = (
        snapshot.get("implicated_paths")
        if isinstance(snapshot.get("implicated_paths"), list)
        else []
    )
    if paths:
        lines.append(
            "- Likely paths from the task statement: %s" % ", ".join(str(p) for p in paths[:12])
        )
    commits = (
        snapshot.get("recent_commits") if isinstance(snapshot.get("recent_commits"), list) else []
    )
    if commits:
        lines.append("- Recent commits that touched those paths (newest first):")
        for item in commits[:8]:
            if not isinstance(item, Mapping):
                continue
            touched = item.get("paths") if isinstance(item.get("paths"), list) else []
            extra = (" " + ",".join(str(p) for p in touched[:4])) if touched else ""
            lines.append(
                "    %s %s%s"
                % ((_text(item.get("sha")) or "?")[:12], _text(item.get("subject")), extra)
            )
    landings = (
        snapshot.get("sibling_landings")
        if isinstance(snapshot.get("sibling_landings"), list)
        else []
    )
    if landings:
        lines.append(
            "- RECENT LANDINGS in this repository (not this task). Re-read HEAD. "
            "Do not treat these as already_published for this task:"
        )
        for item in landings[:6]:
            if not isinstance(item, Mapping):
                continue
            pr = item.get("pr_number")
            pr_bit = (" pr #%s" % pr) if pr not in (None, "", 0) else ""
            lines.append(
                "    [%s]%s task=%s sha=%s"
                % (
                    _text(item.get("event_type")) or "git",
                    pr_bit,
                    _text(item.get("task_id")) or "?",
                    (_text(item.get("sha")) or "?")[:12],
                )
            )
    return "\n".join(lines)


def _reconcile_block(manifest: Mapping[str, Any]) -> Dict[str, Any]:
    block = manifest.get("canonical_reconcile")
    return dict(block) if isinstance(block, Mapping) else {}


def reconcile_evidence_problems(
    manifest: Mapping[str, Any],
    evidence_type: str,
    expected_head_sha: str,
) -> List[str]:
    """Host gate. Empty expected_head_sha means this run did not prepare a snapshot."""

    expected = _text(expected_head_sha)
    if not expected:
        return []
    kind = _text(evidence_type).lower()
    if kind not in {"repo_change", "no_change"}:
        return []
    block = _reconcile_block(manifest)
    problems: List[str] = []
    if not block:
        problems.append(
            "repository evidence requires canonical_reconcile "
            "(still_valid | already_satisfied | needs_restatement) against prepared HEAD"
        )
        return problems
    decision = _text(block.get("decision")).lower()
    if decision not in RECONCILE_DECISIONS:
        problems.append(
            "canonical_reconcile.decision must be still_valid, already_satisfied, "
            "or needs_restatement"
        )
    if not _text(block.get("reason")):
        problems.append("canonical_reconcile.reason is required")
    cited = _text(block.get("head_sha"))
    if cited and not _GIT_SHA.match(cited):
        problems.append("canonical_reconcile.head_sha must be a git SHA")
    elif cited and not head_sha_matches(cited, expected):
        problems.append("canonical_reconcile.head_sha must match the prepared canonical HEAD")
    elif not cited:
        problems.append("canonical_reconcile.head_sha is required")
    if kind == "repo_change" and decision and decision not in REPO_CHANGE_DECISIONS:
        problems.append("repo_change evidence requires canonical_reconcile.decision=still_valid")
    if kind == "no_change" and decision and decision not in NO_CHANGE_DECISIONS:
        problems.append(
            "no_change evidence requires canonical_reconcile.decision="
            "already_satisfied or needs_restatement"
        )
    return problems


def head_sha_matches(cited: str, expected: str) -> bool:
    left = _text(cited).lower()
    right = _text(expected).lower()
    if not left or not right:
        return False
    if left == right:
        return True
    # Permit abbreviated citations of the prepared SHA.
    n = min(len(left), len(right))
    return n >= 7 and (left.startswith(right[:n]) or right.startswith(left[:n]))


def is_no_change_reconcile(manifest: Mapping[str, Any]) -> bool:
    kind = _text(manifest.get("evidence_type")).lower()
    decision = _text(_reconcile_block(manifest).get("decision")).lower()
    return kind == "no_change" and decision in NO_CHANGE_DECISIONS


def host_still_valid_reconcile(
    task: Optional[Mapping[str, Any]],
    existing: Optional[Mapping[str, Any]] = None,
    *,
    reason: str = "",
) -> Dict[str, Any]:
    """Fill still_valid when the host is publishing a repo_change.

    The look is the snapshot attached at worktree prep. Host rescue of a
    missing/incomplete agent manifest still needs a decision that cites that
    HEAD, or submit refuses. Do not invent already_satisfied here.
    """
    block = dict(existing) if isinstance(existing, Mapping) else {}
    if _text(block.get("decision")).lower() in RECONCILE_DECISIONS:
        return block
    snapshot = reconcile_snapshot_from_task(task)
    head = _text(block.get("head_sha")) or _text(snapshot.get("head_sha"))
    if not head:
        return block
    block["decision"] = "still_valid"
    block["head_sha"] = head
    if not _text(block.get("reason")):
        block["reason"] = reason or (
            "repository change finalized against prepared canonical HEAD %s" % head[:12]
        )
    return block
