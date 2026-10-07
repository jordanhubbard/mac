"""An independent judge: does this change do what the task asked?

The coding agent's word that it is done is not evidence, and neither are the
harness's bookkeeping fields. The judge is a separate model call that reads
the task (its description and acceptance criteria), the change itself, what
the agent said it did, and the repository gate's result, and answers ``met``
or ``not_met``. A ``not_met`` verdict comes with ``next``: concrete steps that
the executor hands straight back to the same agent session, so the work is
continued, not thrown away and restarted.

The judge never edits anything. When it cannot run (no hub, no model, an
unparseable reply) it says ``unavailable`` and the executor proceeds exactly
as it would have without it; an absent judge must not fail good work.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional

JUDGE_MODEL_ENV = "MAC_JUDGE_MODEL"
DEFAULT_JUDGE_MODEL = "claude-opus-4-8"
MAX_DIFF_CHARS = 60_000
MAX_UNTRACKED_FILE_CHARS = 8_000
MAX_SUMMARY_CHARS = 6_000

VERDICTS = ("met", "not_met", "unavailable")


@dataclass(frozen=True)
class Verdict:
    verdict: str
    reason: str
    next_steps: str = ""
    model: str = ""
    raw: str = ""
    problems: List[str] = field(default_factory=list)

    @property
    def met(self) -> bool:
        return self.verdict == "met"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": "mac.task_judge_verdict.v1",
            "verdict": self.verdict,
            "reason": self.reason,
            "next": self.next_steps,
            "model": self.model,
            "problems": list(self.problems),
        }


def _git(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    return completed.stdout if completed.returncode == 0 else ""


def collect_change(worktree: Path, base_sha: str) -> str:
    """The agent's change as text: tracked diff against base, then new files.

    Read-only: it never stages anything, so the finalizer's view of new files
    is unchanged.
    """
    worktree = Path(worktree)
    parts: List[str] = []
    stat = _git(worktree, "diff", "--stat", base_sha) if base_sha else _git(worktree, "diff", "--stat")
    if stat.strip():
        parts.append("## diff --stat\n" + stat)
    diff = _git(worktree, "diff", base_sha) if base_sha else _git(worktree, "diff")
    if diff.strip():
        parts.append("## diff\n" + diff)
    untracked = [
        line for line in _git(worktree, "ls-files", "--others", "--exclude-standard").splitlines() if line
    ]
    for name in untracked[:40]:
        path = worktree / name
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            parts.append("## new file %s (binary or unreadable)" % name)
            continue
        if len(text) > MAX_UNTRACKED_FILE_CHARS:
            text = text[:MAX_UNTRACKED_FILE_CHARS] + "\n... (truncated)"
        parts.append("## new file %s\n%s" % (name, text))
    if len(untracked) > 40:
        parts.append("## and %d more new files" % (len(untracked) - 40))
    change = "\n\n".join(parts)
    if len(change) > MAX_DIFF_CHARS:
        change = change[:MAX_DIFF_CHARS] + "\n... (change truncated at %d characters)" % MAX_DIFF_CHARS
    return change


def _task_text(task: Mapping[str, Any]) -> str:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    lines = ["Title: %s" % task.get("title", ""), "", str(task.get("description") or "").strip()]
    acceptance = metadata.get("acceptance_criteria") or metadata.get("acceptance")
    if acceptance:
        lines += ["", "Acceptance criteria:", json.dumps(acceptance, indent=2) if not isinstance(acceptance, str) else acceptance]
    return "\n".join(lines).strip()


def build_prompt(
    task: Mapping[str, Any],
    change: str,
    *,
    agent_summary: str = "",
    gate_summary: str = "",
    board_direction: str = "",
) -> str:
    sections = [
        "You are the independent judge for one task in an automated coding system. A coding "
        "agent worked on the task below. Decide whether its change does what the task asks: "
        "every numbered requirement and every acceptance criterion, including what they imply. "
        "Judge the change itself, not the agent's description of it. Do not reject work for "
        "style, for scope you would have chosen differently, or for anything the task did not "
        "ask for. If nothing needed to change and the agent explained why convincingly, that "
        "is met.",
        "# Task\n" + _task_text(task),
    ]
    if board_direction.strip():
        sections.append("# Direction the task owner gave during the work\n" + board_direction.strip())
    sections.append(
        "# What the agent says it did\n" + (agent_summary.strip()[:MAX_SUMMARY_CHARS] or "(nothing)")
    )
    sections.append("# Repository gate\n" + (gate_summary.strip() or "(not run)"))
    sections.append("# The change\n" + (change.strip() or "(no change to the repository)"))
    sections.append(
        "# Answer\nReply with one JSON object and nothing else:\n"
        '{"verdict": "met" | "not_met", "reason": "<one or two sentences>", '
        '"next": "<for not_met: the specific steps that would make it met; empty for met>"}'
    )
    return "\n\n".join(sections)


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_reply(text: str, *, model: str = "") -> Verdict:
    match = _JSON_OBJECT.search(text or "")
    if not match:
        return Verdict("unavailable", "the judge's reply was not JSON", model=model, raw=text[:2000])
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return Verdict("unavailable", "the judge's reply was not valid JSON", model=model, raw=text[:2000])
    verdict = str(data.get("verdict") or "").strip().lower()
    if verdict not in ("met", "not_met"):
        return Verdict("unavailable", "the judge gave no verdict", model=model, raw=text[:2000])
    next_steps = str(data.get("next") or "").strip()
    if verdict == "not_met" and not next_steps:
        next_steps = str(data.get("reason") or "").strip()
    return Verdict(verdict, str(data.get("reason") or "").strip(), next_steps, model=model, raw=text[:2000])


Completion = Callable[[str, str], str]


def hub_completion(base_url: str, token: str, *, timeout: float = 300.0) -> Completion:
    """A completion function over the hub router's chat endpoint."""

    def _complete(model: str, prompt: str) -> str:
        body = json.dumps(
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 2000,
                "temperature": 0,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            base_url.rstrip("/") + "/v1/chat/completions",
            data=body,
            method="POST",
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8"))
        return str(payload["choices"][0]["message"]["content"] or "")

    return _complete


def judge(
    task: Mapping[str, Any],
    change: str,
    complete: Completion,
    *,
    model: str = DEFAULT_JUDGE_MODEL,
    agent_summary: str = "",
    gate_summary: str = "",
    board_direction: str = "",
) -> Verdict:
    prompt = build_prompt(
        task,
        change,
        agent_summary=agent_summary,
        gate_summary=gate_summary,
        board_direction=board_direction,
    )
    try:
        reply = complete(model, prompt)
    except Exception as exc:  # noqa: BLE001 - an absent judge must not fail the work
        return Verdict("unavailable", "the judge could not be reached: %s" % type(exc).__name__, model=model)
    return parse_reply(reply, model=model)
