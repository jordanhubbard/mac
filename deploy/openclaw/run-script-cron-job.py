#!/usr/bin/env python3
"""Host-side two-stage cron runner for migrated Hermes jobs.

The Hermes->OpenClaw gateway migration (2026-07-12) re-homed cron jobs into the
OpenClaw sandbox.  Hermes ran certain jobs as *two-stage* jobs: a pre-run
script executed on the HOST (e.g. ``~/.hermes/scripts/dream_cycle.py``, which
reads the Hermes session DB under ``~/.hermes/``) and its stdout was injected
into the agent prompt under a ``## Script Output`` heading; the agent's reply
was then delivered to a Slack origin channel.

OpenClaw cron is message-only and runs inside a sandbox that has neither the
host script nor its data sources, so the migrated jobs fired against a prompt
that referenced a dream log that was never produced.  The committed guardrail in
``apply-cron-plan.mjs`` installs such jobs DISABLED.  This runner *restores* them
by reproducing the two-stage flow on the host, where the scripts and data live:

  1. run the host script and capture its stdout (bounded, timed out);
  2. build the combined prompt (job message + ``## Script Output`` section);
  3. run one agent turn via the host ``openclaw-agent`` wrapper;
  4. deliver the reply to the job's Slack channel via the ``openclaw-message``
     wrapper, or, when there is no deliverable target, write it locally.

The pure core (prompt building, reply extraction, deliver-vs-local decision) is
kept free of subprocess calls so it is unit-testable; the three external calls
(script, agent, deliver) are injected seams that default to thin subprocess
wrappers.  Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Any, Callable, Optional, Tuple

SCHEMA = "mac.openclaw_host_script_jobs.v1"

# Bound the injected script output so a runaway script cannot blow the prompt
# (and the router context window) up.
MAX_SCRIPT_OUTPUT = 20000
DEFAULT_SCRIPT_TIMEOUT = 300.0
DEFAULT_AGENT_TIMEOUT = 300.0
SCRIPT_OUTPUT_HEADING = "## Script Output"

# A Slack channel/group/DM id, matching apply-cron-plan.mjs's deliveryArgs.
_SLACK_TARGET = re.compile(r"[CGD][A-Z0-9]+")


# --------------------------------------------------------------------------- #
# Pure core (no subprocess) — unit-testable                                    #
# --------------------------------------------------------------------------- #
def build_prompt(
    message: str,
    script_output: str = "",
    script_note: str = "",
    *,
    script_name: str = "",
    max_output: int = MAX_SCRIPT_OUTPUT,
) -> str:
    """Combine the job message with a ``## Script Output`` section.

    ``script_output`` is the captured stdout when the script ran; ``script_note``
    is an explicit ``(script <name> unavailable: <reason>)`` message used when the
    script was missing or failed.  Exactly one of them populates the section, so
    the agent never sees a phantom reference to output that was never produced.
    A message-only job (no script name, no output, no note) gets no section.
    """
    message = (message or "").rstrip()
    body = ""
    if script_output:
        body = script_output.strip()
        if len(body) > max_output:
            body = body[:max_output].rstrip() + "\n\n[output truncated]"
    elif script_note:
        body = script_note.strip()
    if not body and not script_name:
        return message
    if not body:
        # A script was expected but produced nothing and left no note.
        body = "(script %s produced no output)" % (script_name or "unknown")
    return "%s\n\n%s\n\n%s\n" % (message, SCRIPT_OUTPUT_HEADING, body)


def extract_reply(payload: Any) -> str:
    """Tolerantly pull the agent's reply text out of ``openclaw agent --json``.

    Mirrors worker_reflect._run_reflect_query: recurse through the nested
    text/response/content/message keys (and payloads/messages/result/data
    containers).  Accepts a decoded object or a raw JSON string; a non-JSON
    string is returned as-is.
    """
    if isinstance(payload, str):
        stripped = payload.strip()
        if not stripped:
            return ""
        try:
            payload = json.loads(stripped)
        except (TypeError, ValueError):
            return stripped

    def response_text(value: Any) -> str:
        if isinstance(value, dict):
            for key in ("text", "response", "content", "message"):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
                nested = response_text(candidate)
                if nested:
                    return nested
            for key in ("payloads", "messages", "result", "data"):
                nested = response_text(value.get(key))
                if nested:
                    return nested
        elif isinstance(value, list):
            for item in value:
                nested = response_text(item)
                if nested:
                    return nested
        return ""

    text = response_text(payload)
    if text:
        return text
    if isinstance(payload, str):
        return payload.strip()
    return ""


def resolve_delivery_target(job: dict) -> Optional[Tuple[str, str]]:
    """Return ``(platform, channel_id)`` to deliver to, or ``None`` for local.

    Deliberately identical in spirit to apply-cron-plan.mjs's ``deliveryArgs``:
    ``local`` (or an unaddressed job) stays local; a durable Slack channel id is
    delivered; a legacy delivery target that is a human name OpenClaw cannot
    resolve falls back to local so the reply is never sent to the wrong channel.
    """
    delivery = str(job.get("delivery") or "")
    origin = job.get("origin") if isinstance(job.get("origin"), dict) else {}
    platform = str(origin.get("platform") or "")
    if delivery == "local" or (not delivery and not platform):
        return None
    if delivery.startswith("slack:") or platform == "slack":
        raw = delivery[len("slack:"):] if delivery.startswith("slack:") else ""
        target = str(origin.get("chat_id") or raw or "").strip()
        if _SLACK_TARGET.fullmatch(target):
            return ("slack", target)
    return None


# --------------------------------------------------------------------------- #
# Default subprocess seams (thin, injected)                                    #
# --------------------------------------------------------------------------- #
def default_script_runner(
    scripts_dir: str,
    script_name: str,
    *,
    timeout: float = DEFAULT_SCRIPT_TIMEOUT,
) -> Tuple[str, str]:
    """Run ``<scripts_dir>/<script_name>`` and return ``(stdout, note)``.

    On success ``note`` is empty.  A missing script or a failure yields an empty
    stdout and an explicit ``(script <name> unavailable: <reason>)`` note — never
    a phantom reference.
    """
    path = Path(scripts_dir).expanduser() / script_name
    if not path.is_file():
        return "", "(script %s unavailable: not found at %s)" % (script_name, path)
    try:
        result = subprocess.run(
            [sys.executable or "python3", str(path)],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "", "(script %s unavailable: timed out after %ds)" % (script_name, int(timeout))
    except Exception as exc:  # noqa: BLE001 - the runner must not crash on a bad script
        return "", "(script %s unavailable: %s)" % (script_name, exc)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[:300]
        return "", "(script %s unavailable: exited %d%s)" % (
            script_name,
            result.returncode,
            (": " + detail) if detail else "",
        )
    return (result.stdout or "").strip(), ""


def default_agent_runner(
    agent_bin: str,
    prompt: str,
    *,
    session_id: str = "",
    timeout: float = DEFAULT_AGENT_TIMEOUT,
) -> str:
    """Run one agent turn through the host ``openclaw-agent`` wrapper (--json)."""
    args = [agent_bin, "--agent", "main", "--message", prompt]
    if session_id:
        args += ["--session-id", session_id]
    args += ["--json"]
    result = subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0 and not output:
        return (result.stderr or "").strip()
    return output


def default_deliver_runner(
    message_bin: str,
    target: Tuple[str, str],
    text: str,
    *,
    account: str = "default",
) -> None:
    """Deliver ``text`` to a Slack channel via the host ``openclaw-message`` wrapper."""
    platform, channel_id = target
    subprocess.run(
        message_args(message_bin, platform, channel_id, text, account=account),
        check=True,
    )


def message_args(
    message_bin: str,
    platform: str,
    channel_id: str,
    text: str,
    *,
    account: str = "default",
) -> list:
    """Build the ``openclaw-message`` argv (kept pure so it is testable).

    The body travels as JSON in ``--presentation``, never as ``--message``.

    ``openclaw-message`` is a five-line wrapper that execs
    ``openshell sandbox exec ... -- openclaw message "$@"``, and OpenShell's exec
    transport REFUSES any argv token containing a newline:

        code: 'Client specified an invalid argument'
        message: "command argument 12 contains newline or carriage return characters"

    So the limit is the sandbox boundary, not the chat CLI, and it applies to
    every multi-line payload regardless of which tool is on the far side. Script
    job output is prose and is therefore always multi-line: on the hub, three
    jobs ran hourly and failed 220 times each on exactly this.

    ``json.dumps`` escapes the newlines, so the token crossing the boundary has
    none while the body survives intact. ``--message`` keeps a single-line
    summary because the CLI requires it.
    """
    return [
        message_bin,
        "send",
        "--channel",
        platform,
        "--account",
        account,
        "--target",
        "channel:%s" % channel_id,
        "--message",
        summary_line(text),
        "--presentation",
        json.dumps({"text": text}),
    ]


def summary_line(text: str, *, limit: int = 200) -> str:
    """A single-line stand-in for a multi-line body.

    Never empty: the CLI rejects a missing message, and a job whose output was
    whitespace would otherwise fail for a second, unrelated reason.
    """
    first = ""
    for line in str(text or "").splitlines():
        if line.strip():
            first = line.strip()
            break
    if not first:
        return "(no summary)"
    return first[:limit]


# --------------------------------------------------------------------------- #
# Orchestration                                                                #
# --------------------------------------------------------------------------- #
def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-") or "job"


def write_local_output(output_dir: str, job: dict, prompt: str, reply: str) -> str:
    """Persist a non-deliverable reply to a local markdown file; return its path."""
    directory = Path(output_dir).expanduser()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    destination = directory / ("%s-%s.md" % (_slug(job.get("name") or "job"), stamp))
    destination.write_text(
        "# %s\n\n_%s_\n\n## Prompt\n\n%s\n\n## Reply\n\n%s\n"
        % (job.get("name") or "job", stamp, prompt, reply),
        encoding="utf-8",
    )
    try:
        os.chmod(destination, 0o600)
    except OSError:
        pass
    return str(destination)


def run_job(
    job: dict,
    *,
    scripts_dir: str,
    agent_bin: str,
    message_bin: str,
    output_dir: str,
    account: str = "default",
    script_runner: Callable[..., Tuple[str, str]] = default_script_runner,
    agent_runner: Callable[..., str] = default_agent_runner,
    deliver_runner: Callable[..., None] = default_deliver_runner,
) -> dict:
    """Reproduce the Hermes two-stage flow for one job. Returns a result dict."""
    message = str(job.get("message") or "")
    legacy_script = str(job.get("legacy_script") or job.get("script") or "").strip()

    script_output, script_note = "", ""
    if legacy_script:
        script_output, script_note = script_runner(scripts_dir, legacy_script)

    prompt = build_prompt(
        message, script_output, script_note, script_name=legacy_script
    )

    session_id = "mac-host-cron-%s" % _slug(job.get("name") or job.get("legacy_id") or "job")
    reply = extract_reply(agent_runner(agent_bin, prompt, session_id=session_id))

    target = resolve_delivery_target(job)
    result: dict = {
        "name": job.get("name"),
        "legacy_script": legacy_script or None,
        "script_ran": bool(legacy_script and not script_note),
        "script_note": script_note or None,
        "delivered": False,
        "target": None,
        "local_path": None,
        "reply_chars": len(reply),
    }
    if target and reply:
        deliver_runner(message_bin, target, reply, account=account)
        result["delivered"] = True
        result["target"] = "%s:%s" % target
    else:
        result["local_path"] = write_local_output(output_dir, job, prompt, reply)
    return result


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #
def load_job(args: argparse.Namespace) -> dict:
    """Resolve the job spec from a jobs-file+name, a single spec file, or flags."""
    if args.jobs_file and args.name:
        raw = json.loads(Path(args.jobs_file).expanduser().read_text(encoding="utf-8"))
        jobs = raw.get("jobs") if isinstance(raw, dict) else raw
        if not isinstance(jobs, list):
            jobs = []
        for job in jobs:
            if isinstance(job, dict) and str(job.get("name") or "") == args.name:
                return job
        raise SystemExit("job %r not found in %s" % (args.name, args.jobs_file))
    if args.spec:
        job = json.loads(Path(args.spec).expanduser().read_text(encoding="utf-8"))
        if not isinstance(job, dict):
            raise SystemExit("job spec %s is not a JSON object" % args.spec)
        return job
    if not args.name:
        raise SystemExit("a job requires --spec, --jobs-file+--name, or --name+--script")
    origin = None
    delivery = None
    if args.origin_chat_id:
        origin = {"platform": "slack", "chat_id": args.origin_chat_id}
        delivery = "slack:%s" % args.origin_chat_id
    elif args.deliver:
        delivery = args.deliver
    return {
        "name": args.name,
        "message": args.message or "",
        "legacy_script": args.script or "",
        "delivery": delivery,
        "origin": origin,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("spec", nargs="?", help="path to a single-job JSON spec file")
    result.add_argument("--jobs-file", help="path to a host-script-jobs.json array file")
    result.add_argument("--name", help="job name (selects from --jobs-file, or with --script)")
    result.add_argument("--script", help="legacy script filename under the scripts dir")
    result.add_argument("--message", help="job message / prompt")
    result.add_argument("--deliver", help="raw delivery target, e.g. 'slack:C0123' or 'local'")
    result.add_argument("--origin-chat-id", help="Slack channel id to deliver the reply to")
    result.add_argument("--scripts-dir", help="override the Hermes scripts directory")
    result.add_argument("--agent-bin", help="override the openclaw-agent wrapper path")
    result.add_argument("--message-bin", help="override the openclaw-message wrapper path")
    result.add_argument("--account", help="Slack account id for delivery")
    result.add_argument("--output-dir", help="where non-deliverable replies are written")
    return result


def _default_home_bin(name: str) -> str:
    return str(Path.home() / ".mac" / "bin" / name)


def main(argv: Optional[list] = None) -> int:
    args = parser().parse_args(argv)
    job = load_job(args)

    scripts_dir = (
        args.scripts_dir
        or os.environ.get("MAC_HERMES_SCRIPTS_DIR")
        or str(Path.home() / ".hermes" / "scripts")
    )
    agent_bin = (
        args.agent_bin
        or os.environ.get("MAC_OPENCLAW_AGENT_BIN")
        or _default_home_bin("openclaw-agent")
    )
    message_bin = (
        args.message_bin
        or os.environ.get("MAC_OPENCLAW_MESSAGE_BIN")
        or _default_home_bin("openclaw-message")
    )
    account = (
        args.account or os.environ.get("MAC_OPENCLAW_SLACK_ACCOUNT_ID") or "default"
    )
    output_dir = (
        args.output_dir
        or os.environ.get("MAC_OPENCLAW_SCRIPT_JOB_OUTPUT_DIR")
        or str(Path.home() / ".mac" / "openclaw" / "script-jobs" / "output")
    )

    result = run_job(
        job,
        scripts_dir=scripts_dir,
        agent_bin=agent_bin,
        message_bin=message_bin,
        output_dir=output_dir,
        account=account,
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
