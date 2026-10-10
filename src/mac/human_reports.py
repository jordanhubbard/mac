"""Act, then tell: agents report to people what might concern them.

MAC agents keep their authority. An agent may change a repository setting,
force-push a shared branch or touch a host when its work calls for it; what
it may not do is let that pass silently. The rule, in the owner's words: "do
the thing but tell people afterwards" (2026-10-10). The gap behind it was
2026-10-02, when an agent in a sandbox created an active ruleset on
jordanhubbard/Aviation and no person was told.

Two kinds of report:

* ``self`` -- what the reporting agent itself did beyond its own task branch
  and pull request, why, and how to undo it;
* ``peer`` -- what the reporting agent noticed another agent do that a person
  should know about, naming that agent or task, with the evidence.

A report is one ``operator_notifications`` row (event ``agent.report``), so
it reaches Slack through the notifier and the hub-agent heartbeat drain
exactly like a task question does, and an observability log. The row is
written when the report is filed, so the report is delivered even if the
reporting task fails afterwards. A dedupe key makes filing idempotent: the
same report filed twice (an agent repeating itself, two workers detecting
the same repository change) notifies people once.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Mapping, Optional

from mac.models import ValidationError

#: The notification event every report is filed under.
REPORT_EVENT = "agent.report"

#: The observability log for every filed report (duplicates included).
REPORT_LOG = "agent.report.filed"

REPORT_KINDS = ("self", "peer")

#: Where a coding agent that has no board command writes its reports: one
#: JSON object per line in its task workspace. The worker files each one
#: with the hub when the attempt ends, whatever its outcome.
WORKSPACE_REPORTS_FILE = "human-reports.jsonl"

MAX_FIELD_CHARS = 2000
MAX_REPORTS_PER_ATTEMPT = 20

#: The rule every agent is told, verbatim, wherever agents get instructions.
ACT_THEN_TELL_RULE = (
    "Act, then tell. You keep your authority: do what the work needs. But "
    "whenever you do something a person might want to know about -- anything "
    "beyond your own task branch and pull request, such as changing repository "
    "settings, rulesets, branch protection, webhooks, secrets or collaborators; "
    "force-pushing or deleting a shared or canonical branch; closing someone "
    "else's pull request or issue; changing a host, service or infrastructure; "
    "or spending money or causing an external side effect -- report it to the "
    "humans right afterwards: what you did, why, and how to undo it. Routine "
    "task work (your branch, your PR, running tests) needs no report.\n"
    "Tell on others too. If you notice another agent doing something a person "
    "should know about -- an unexpected settings change, a destructive git "
    "operation, work that contradicts its task, or a result it claimed that did "
    "not happen -- report it, naming the agent or task and the evidence."
)


def _clip(value: Any) -> str:
    return str(value or "").strip()[:MAX_FIELD_CHARS]


def normalize_report(
    body: str,
    *,
    report: str = "self",
    why: Any = None,
    undo: Any = None,
    about_agent: Any = None,
    about_task: Any = None,
    evidence: Any = None,
    key: Any = None,
) -> Dict[str, str]:
    """Validate one report; return its fields, empty ones dropped."""
    text = _clip(body)
    if not text:
        raise ValidationError("a report needs a body: what happened")
    kind = str(report or "self").strip().lower()
    if kind not in REPORT_KINDS:
        raise ValidationError("report must be one of %s" % ", ".join(REPORT_KINDS))
    fields = {
        "report": kind,
        "body": text,
        "why": _clip(why),
        "undo": _clip(undo),
        "about_agent": _clip(about_agent),
        "about_task": _clip(about_task),
        "evidence": _clip(evidence),
        "key": _clip(key),
    }
    if kind == "peer" and not (fields["about_agent"] or fields["about_task"]):
        raise ValidationError("a peer report must name the agent or task it is about")
    return {name: value for name, value in fields.items() if value}


def report_from_metadata(body: str, metadata: Optional[Mapping[str, Any]]) -> Dict[str, str]:
    """A report carried as a task-board message (kind ``report``)."""
    meta = dict(metadata or {})
    return normalize_report(
        body,
        report=meta.get("report") or "self",
        why=meta.get("why"),
        undo=meta.get("undo"),
        about_agent=meta.get("about_agent"),
        about_task=meta.get("about_task"),
        evidence=meta.get("evidence"),
        key=meta.get("key"),
    )


def dedupe_key(report: Mapping[str, str], *, reporter: str, task_id: str = "") -> str:
    """The identity two filings of the same report share.

    An explicit ``key`` wins and is global, so two reporters that detect the
    same event (say, one repository change) can agree on it. Otherwise a
    report is the same report when the same reporter files the same words
    about the same subject from the same task.
    """
    explicit = str(report.get("key") or "").strip()
    if explicit:
        return "key:%s" % explicit
    material = json.dumps(
        {
            "reporter": reporter,
            "task_id": task_id,
            "report": report.get("report"),
            "about_agent": report.get("about_agent", ""),
            "about_task": report.get("about_task", ""),
            "body": " ".join(str(report.get("body") or "").split()).lower(),
        },
        sort_keys=True,
    )
    return "sha256:%s" % hashlib.sha256(material.encode("utf-8")).hexdigest()


def notification_text(
    report: Mapping[str, str],
    *,
    reporter: str,
    task_id: str = "",
    task_title: str = "",
) -> Dict[str, str]:
    """The title and body people read in Slack."""
    where = ""
    if task_id:
        where = " on %s" % (task_title and "%s (%s)" % (task_title, task_id) or task_id)
    if report.get("report") == "peer":
        subject = report.get("about_agent") or report.get("about_task") or "another agent"
        title = "Agent report: %s flagged %s%s" % (reporter, subject, where)
    else:
        title = "Agent report: %s acted%s" % (reporter, where)
    lines = [str(report.get("body") or "")]
    if report.get("about_agent"):
        lines.append("About agent: %s" % report["about_agent"])
    if report.get("about_task"):
        lines.append("About task: %s" % report["about_task"])
    if report.get("why"):
        lines.append("Why: %s" % report["why"])
    if report.get("evidence"):
        lines.append("Evidence: %s" % report["evidence"])
    if report.get("undo"):
        lines.append("To undo: %s" % report["undo"])
    lines.append("Reported by %s%s." % (reporter, where))
    return {"title": title[:300], "body": "\n".join(lines)}


def parse_workspace_reports(text: str) -> list:
    """Reports an agent wrote to ``WORKSPACE_REPORTS_FILE``; bad lines skipped."""
    reports = []
    for line in str(text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if not isinstance(item, dict):
            continue
        body = item.get("body") or item.get("what") or item.get("message")
        try:
            reports.append(report_from_metadata(str(body or ""), item))
        except ValidationError:
            continue
        if len(reports) >= MAX_REPORTS_PER_ATTEMPT:
            break
    return reports
