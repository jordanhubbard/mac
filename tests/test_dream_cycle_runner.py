"""Tests for the dream-cycle RESTORATION mechanism (task_c8bb46ec).

Covers the host-side two-stage runner (deploy/openclaw/run-script-cron-job.py),
the host-runner spec emission in apply-cron-plan.mjs, and the host-side
scheduling wired into install-openclaw-gateway.sh.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OPENCLAW_DIR = ROOT / "deploy" / "openclaw"
RUNNER_PATH = OPENCLAW_DIR / "run-script-cron-job.py"
APPLY_CRON_PLAN = OPENCLAW_DIR / "apply-cron-plan.mjs"
INSTALLER = OPENCLAW_DIR / "install-openclaw-gateway.sh"


def _load_runner():
    # The module file has a hyphen, so import it directly from its path.
    spec = importlib.util.spec_from_file_location("run_script_cron_job", RUNNER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


# --------------------------------------------------------------------------- #
# Pure core                                                                    #
# --------------------------------------------------------------------------- #
def test_build_prompt_includes_script_output_section_and_stdout() -> None:
    prompt = runner.build_prompt(
        "Reflect on last night's dreams.",
        script_output="dream log: chased a deadline made of clocks",
        script_name="dream_cycle.py",
    )
    assert "Reflect on last night's dreams." in prompt
    assert "## Script Output" in prompt
    assert "chased a deadline made of clocks" in prompt


def test_build_prompt_missing_script_uses_explicit_unavailable_note() -> None:
    note = "(script dream_cycle.py unavailable: not found at /nope/dream_cycle.py)"
    prompt = runner.build_prompt(
        "Reflect on last night's dreams.",
        script_output="",
        script_note=note,
        script_name="dream_cycle.py",
    )
    assert "## Script Output" in prompt
    # The explicit unavailable note appears; no phantom dream-log reference.
    assert note in prompt
    assert "unavailable" in prompt


def test_build_prompt_message_only_has_no_script_section() -> None:
    prompt = runner.build_prompt("just a message", script_name="")
    assert "## Script Output" not in prompt
    assert prompt.strip() == "just a message"


def test_build_prompt_bounds_output_length() -> None:
    huge = "x" * 50000
    prompt = runner.build_prompt("m", script_output=huge, script_name="s.py")
    assert "[output truncated]" in prompt
    assert len(prompt) < len(huge)


def test_extract_reply_handles_nested_json() -> None:
    payload = {"result": {"messages": [{"content": "the woven answer"}]}}
    assert runner.extract_reply(payload) == "the woven answer"
    # Also accepts a raw JSON string (as emitted by the wrapper) ...
    assert runner.extract_reply(json.dumps({"text": "hi"})) == "hi"
    # ... and non-JSON text is returned as-is.
    assert runner.extract_reply("plain text reply") == "plain text reply"
    assert runner.extract_reply("") == ""


def test_kslug_process_note_is_not_a_transcript() -> None:
    note = (
        "The KSLUG skill and SPEC aren't accessible in this sandbox — "
        "proceeding from the prompt's embedded cast/format instructions."
    )
    assert runner.kslug_transcript_ok(note) is False
    difficulties = (
        ":rotating_light: *KSLUG TECHNICAL DIFFICULTIES* :rotating_light:\n"
        "The wire is down. Dan and Lee sign off."
    )
    assert runner.kslug_transcript_ok(difficulties) is True


def test_choose_reply_prefers_kslug_transcript_over_preamble() -> None:
    transcript = (
        ":tv: _KSLUG NIGHTLY NEWS_ :tv:\n"
        "_DAN GREEN:_ Good evening, Santa Cruz. I'm Dan Green. " + ("Item from the wire. " * 40)
    )
    payload = {
        "text": "The KSLUG skill and SPEC aren't accessible in this sandbox.",
        "payloads": [{"text": transcript}],
    }
    job = {"name": "kslug-nightly-news"}
    chosen = runner.choose_reply(job, payload)
    assert chosen.startswith(":tv: _KSLUG NIGHTLY NEWS_")
    assert "DAN GREEN" in chosen
    assert len(chosen) >= 500
    assert runner.extract_reply(payload).startswith("The KSLUG skill")


def test_kslug_process_note_fails_closed_without_slack_delivery(tmp_path: Path) -> None:
    delivered = []
    job = {
        "name": "kslug-nightly-news",
        "cron": "0 6 * * *",
        "legacy_script": "kslug_collect.py",
        "message": "Produce the broadcast.",
        "delivery": "slack:C0HOME",
        "authorized_slack_channels": ["channel:C0HOME"],
    }
    result = runner.run_job(
        job,
        scripts_dir=str(tmp_path),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(tmp_path / "out"),
        home_channel_target="channel:C0HOME",
        script_runner=lambda *_args: ("wire copy", ""),
        agent_runner=lambda *_args, **_kwargs: json.dumps(
            {
                "text": (
                    "The KSLUG skill and SPEC aren't accessible in this sandbox "
                    "— proceeding from the prompt."
                )
            }
        ),
        deliver_runner=lambda *_args, **_kwargs: delivered.append(True),
    )
    assert result["delivered"] is False
    assert result["delivery_refusal"] == "process_note_not_broadcast"
    assert delivered == []
    assert Path(result["local_path"]).is_file()
    assert not (tmp_path / "out" / "kslug-nightly-news.last-success.json").exists()


def test_kslug_prompt_includes_host_runner_contract(tmp_path: Path) -> None:
    captured = {}
    transcript = (
        ":tv: _KSLUG NIGHTLY NEWS_ :tv:\n"
        "_DAN GREEN:_ Good evening, Santa Cruz. I'm Dan Green. " + ("Local wire copy. " * 40)
    )
    job = {
        "name": "kslug-nightly-news",
        "cron": "0 6 * * *",
        "legacy_script": "kslug_collect.py",
        "message": "Produce tonight's broadcast.",
        "delivery": "slack:C0HOME",
        "authorized_slack_channels": ["channel:C0HOME"],
    }
    runner.run_job(
        job,
        scripts_dir=str(tmp_path),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(tmp_path / "out"),
        home_channel_target="channel:C0HOME",
        script_runner=lambda *_args: ("wire", ""),
        agent_runner=lambda _bin, prompt, **_kwargs: (
            captured.update(prompt=prompt) or json.dumps({"text": transcript})
        ),
        deliver_runner=lambda *_args, **_kwargs: None,
    )
    assert "Host-runner contract" in captured["prompt"]
    assert "Do not use Slack or message tools" in captured["prompt"]


def test_resolve_delivery_target_decision() -> None:
    slack_origin = {"origin": {"platform": "slack", "chat_id": "C0123ABC"}}
    assert runner.resolve_delivery_target(slack_origin) == ("slack", "C0123ABC")
    assert runner.resolve_delivery_target({"delivery": "slack:G9ZZ99Z"}) == ("slack", "G9ZZ99Z")
    # Explicit local, or unaddressed, => no delivery target.
    assert runner.resolve_delivery_target({"delivery": "local"}) is None
    assert runner.resolve_delivery_target({}) is None
    # A human name OpenClaw cannot resolve durably => local, never wrong channel.
    assert runner.resolve_delivery_target({"delivery": "slack:@jordan"}) is None


def test_delivery_policy_allows_dm_and_home_channel_only() -> None:
    home = "channel:C0HOME"
    assert runner.authorize_delivery_target(("slack", "D0JORDAN"), home) == (
        ("slack", "D0JORDAN"),
        "",
    )
    assert runner.authorize_delivery_target(("slack", "C0HOME"), home) == (
        ("slack", "C0HOME"),
        "",
    )
    assert runner.authorize_delivery_target(("slack", "C0OTHER"), home) == (
        None,
        "slack_broadcast_outside_home_channel",
    )
    assert runner.authorize_delivery_target(("slack", "G0OTHER"), home) == (
        None,
        "slack_broadcast_outside_home_channel",
    )
    assert runner.authorize_delivery_target(
        ("slack", "C0LOCALNEWS"),
        home,
        ["channel:C0LOCALNEWS"],
    ) == (("slack", "C0LOCALNEWS"), "")


def test_message_args_targets_channel_with_account_and_text() -> None:
    args = runner.message_args(
        "/bin/openclaw-message", "slack", "C0123ABC", "hello", account="acct1"
    )
    assert args[0] == "/bin/openclaw-message"
    assert "--channel" in args and "slack" in args
    assert "--account" in args and "acct1" in args
    assert "channel:C0123ABC" in args
    assert "hello" in args


def test_no_argument_ever_carries_a_newline_across_the_sandbox_boundary() -> None:
    """The defect that stopped every script job on the fleet.

    ``openclaw-message`` execs ``openshell sandbox exec ... -- openclaw message``,
    and OpenShell refuses any argv token containing a newline:
    "command argument 12 contains newline or carriage return characters".
    Script-job output is prose, so it is always multi-line. Three jobs on the hub
    ran hourly and failed 220 times each on exactly this -- and the runner then
    reported each failure through the same channel, so the error report failed
    identically and nothing was ever notified.
    """
    body = "first line\nsecond line\r\nthird line"
    args = runner.message_args("/bin/openclaw-message", "slack", "C0123ABC", body, account="a")

    assert not any("\n" in part or "\r" in part for part in args), (
        "an argv token still carries a literal newline, so the sandbox will refuse it"
    )


def test_the_multi_line_body_survives_the_escaping() -> None:
    """Escaping that loses the body would trade a loud failure for a quiet one."""
    import json as _json

    body = "first line\nsecond line\nthird line"
    args = runner.message_args("/bin/openclaw-message", "slack", "C0123ABC", body, account="a")
    presentation = args[args.index("--presentation") + 1]

    assert _json.loads(presentation)["text"] == body


def test_the_summary_line_is_never_empty() -> None:
    """The CLI rejects a missing message, so whitespace-only output must not
    fail for a second, unrelated reason."""
    assert runner.summary_line("   \n\n  ") == "(no summary)"
    assert runner.summary_line("\n\nreal first line\nsecond") == "real first line"


# --------------------------------------------------------------------------- #
# Whole flow with all subprocess seams mocked                                  #
# --------------------------------------------------------------------------- #
def test_run_job_full_flow_delivers_to_slack_channel(tmp_path: Path) -> None:
    captured = {}

    def fake_script(scripts_dir, script_name):
        assert script_name == "dream_cycle.py"
        return ("REM cycle 4: flying over a library", "")

    def fake_agent(agent_bin, prompt, *, session_id="", **kwargs):
        # The agent turn sees both the message and the injected script output.
        assert "## Script Output" in prompt
        assert "flying over a library" in prompt
        captured["prompt"] = prompt
        return json.dumps({"payloads": [{"text": "Tonight you dreamed of flight."}]})

    def fake_deliver(message_bin, target, text, *, account="default"):
        captured["deliver"] = (message_bin, target, text, account)

    job = {
        "name": "dream-cycle",
        "message": "Compose the morning dream note.",
        "legacy_script": "dream_cycle.py",
        "origin": {"platform": "slack", "chat_id": "C0DREAM01"},
    }
    result = runner.run_job(
        job,
        scripts_dir=str(tmp_path / "scripts"),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(tmp_path / "out"),
        account="dreamteam",
        home_channel_target="channel:C0DREAM01",
        script_runner=fake_script,
        agent_runner=fake_agent,
        deliver_runner=fake_deliver,
    )
    assert result["delivered"] is True
    assert result["target"] == "slack:C0DREAM01"
    assert result["script_ran"] is True
    message_bin, target, text, account = captured["deliver"]
    assert target == ("slack", "C0DREAM01")
    assert text == "Tonight you dreamed of flight."
    assert account == "dreamteam"


def test_run_job_missing_script_fails_closed_without_agent_turn(tmp_path: Path) -> None:
    """A collector failure is evidence-free and must never become agent prose."""

    def fake_agent(agent_bin, prompt, *, session_id="", **kwargs):
        raise AssertionError("failed script must not trigger an agent turn")

    def fake_deliver(*args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("local job must not deliver")

    job = {
        "name": "dream-synthesis",
        "message": "Synthesize the week's dreams.",
        "legacy_script": "dream_synthesis.py",
        "delivery": "local",
    }
    result = runner.run_job(
        job,
        scripts_dir=str(tmp_path / "no-scripts-here"),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(tmp_path / "out"),
        agent_runner=fake_agent,
        deliver_runner=fake_deliver,
    )
    assert result["delivered"] is False
    assert result["script_ran"] is False
    assert "unavailable" in (result["script_note"] or "")
    assert result["reply_chars"] == 0
    local_path = Path(result["local_path"])
    assert local_path.is_file()
    local = local_path.read_text(encoding="utf-8")
    assert "## Script Output" in local
    assert "unavailable" in local


def test_run_job_refuses_non_home_channel_broadcast(tmp_path: Path) -> None:
    delivered = []
    job = {
        "name": "dream-cycle",
        "message": "Compose the morning dream note.",
        "legacy_script": "dream_cycle.py",
        "origin": {"platform": "slack", "chat_id": "C0OTHER"},
    }
    result = runner.run_job(
        job,
        scripts_dir=str(tmp_path),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(tmp_path / "out"),
        home_channel_target="channel:C0HOME",
        script_runner=lambda *_args: ("evidence", ""),
        agent_runner=lambda *_args, **_kwargs: '{"text":"private result"}',
        deliver_runner=lambda *args, **kwargs: delivered.append((args, kwargs)),
    )
    assert result["delivered"] is False
    assert result["delivery_refusal"] == "slack_broadcast_outside_home_channel"
    assert delivered == []
    assert Path(result["local_path"]).is_file()


def test_run_job_uses_fresh_session_per_invocation(tmp_path: Path, monkeypatch) -> None:
    seen = []
    monkeypatch.setattr(runner.time, "strftime", lambda *_args: "20260826T052000Z")
    job = {
        "name": "kslug-nightly-news",
        "message": "Produce the broadcast transcript.",
        "legacy_script": "kslug_collect.py",
        "delivery": "local",
    }
    runner.run_job(
        job,
        scripts_dir=str(tmp_path),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(tmp_path / "out"),
        script_runner=lambda *_args: ("wire copy", ""),
        agent_runner=lambda *_args, **kwargs: (
            seen.append(kwargs["session_id"]) or '{"text":"news"}'
        ),
    )
    assert seen == ["mac-host-cron-kslug-nightly-news-20260826T052000Z"]


def test_default_script_runner_reports_missing_script(tmp_path: Path) -> None:
    out, note = runner.default_script_runner(str(tmp_path), "ghost.py")
    assert out == ""
    assert "ghost.py unavailable" in note


def test_default_script_runner_captures_stdout(tmp_path: Path) -> None:
    script = tmp_path / "kslug_collect.py"
    script.write_text("print('collected 3 headlines')\n", encoding="utf-8")
    out, note = runner.default_script_runner(str(tmp_path), "kslug_collect.py")
    assert note == ""
    assert "collected 3 headlines" in out


def test_load_job_selects_by_name_from_jobs_file(tmp_path: Path) -> None:
    specs = tmp_path / "host-script-jobs.json"
    specs.write_text(
        json.dumps(
            {
                "schema": "mac.openclaw_host_script_jobs.v1",
                "jobs": [
                    {"name": "dream-cycle", "legacy_script": "dream_cycle.py"},
                    {"name": "kslug-nightly-news", "legacy_script": "kslug_collect.py"},
                ],
            }
        ),
        encoding="utf-8",
    )
    args = runner.parser().parse_args(["--jobs-file", str(specs), "--name", "kslug-nightly-news"])
    job = runner.load_job(args)
    assert job["legacy_script"] == "kslug_collect.py"


def test_daily_job_skips_second_broadcast_same_local_day(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    job = {
        "name": "kslug-nightly-news",
        "cron": "0 6 * * *",
        "legacy_script": "kslug_collect.py",
        "message": "Produce the broadcast transcript.",
        "delivery": "slack:C0HOME",
        "authorized_slack_channels": ["channel:C0HOME"],
    }
    runner.write_delivery_receipt(
        job,
        str(output_dir),
        {"target": "slack:C0HOME", "reply_chars": 12},
    )
    called = []
    result = runner.run_job(
        job,
        scripts_dir=str(tmp_path),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(output_dir),
        home_channel_target="channel:C0HOME",
        script_runner=lambda *_args: called.append("script") or ("wire", ""),
        agent_runner=lambda *_args, **_kwargs: called.append("agent") or '{"text":"news"}',
        deliver_runner=lambda *_args, **_kwargs: called.append("deliver"),
    )
    assert result["skipped"] == "already_delivered_today"
    assert result["delivered"] is False
    assert called == []


def test_hourly_job_is_not_day_deduped(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    output_dir.mkdir()
    job = {
        "name": "dream-cycle",
        "cron": "0 * * * *",
        "legacy_script": "dream_cycle.py",
        "message": "Compose the morning dream note.",
        "delivery": "slack:C0HOME",
        "authorized_slack_channels": ["channel:C0HOME"],
    }
    runner.write_delivery_receipt(
        job,
        str(output_dir),
        {"target": "slack:C0HOME", "reply_chars": 12},
    )
    delivered = []
    result = runner.run_job(
        job,
        scripts_dir=str(tmp_path),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(output_dir),
        home_channel_target="channel:C0HOME",
        script_runner=lambda *_args: ("evidence", ""),
        agent_runner=lambda *_args, **_kwargs: '{"text":"note"}',
        deliver_runner=lambda *_args, **_kwargs: delivered.append(True),
    )
    assert "skipped" not in result
    assert result["delivered"] is True
    assert delivered == [True]


def test_successful_daily_delivery_writes_same_day_receipt(tmp_path: Path) -> None:
    output_dir = tmp_path / "out"
    job = {
        "name": "kslug-nightly-news",
        "cron": "0 6 * * *",
        "legacy_script": "kslug_collect.py",
        "message": "Produce the broadcast.",
        "delivery": "slack:C0HOME",
        "authorized_slack_channels": ["channel:C0HOME"],
    }
    result = runner.run_job(
        job,
        scripts_dir=str(tmp_path),
        agent_bin="/bin/openclaw-agent",
        message_bin="/bin/openclaw-message",
        output_dir=str(output_dir),
        home_channel_target="channel:C0HOME",
        script_runner=lambda *_args: ("wire copy", ""),
        agent_runner=lambda *_args, **_kwargs: json.dumps(
            {
                "text": (
                    ":tv: _KSLUG NIGHTLY NEWS_ :tv:\n"
                    "_DAN GREEN:_ Good evening, Santa Cruz. I'm Dan Green "
                    "and this is the KSLUG Nightly News. " + ("Local wire copy. " * 40)
                )
            }
        ),
        deliver_runner=lambda *_args, **_kwargs: None,
    )
    assert result["delivered"] is True
    receipt = json.loads(
        (output_dir / "kslug-nightly-news.last-success.json").read_text(encoding="utf-8")
    )
    assert receipt["schema"] == "mac.host_script_job_delivery.v1"
    assert receipt["local_date"] == runner.calendar_day_key()
    assert receipt["target"] == "slack:C0HOME"


# --------------------------------------------------------------------------- #
# Deploy-artifact string contracts                                            #
# --------------------------------------------------------------------------- #
def test_apply_cron_plan_emits_host_script_jobs_spec() -> None:
    apply = APPLY_CRON_PLAN.read_text(encoding="utf-8")
    # A host-runner spec array is collected and written next to the plan.
    assert "hostScriptJobs" in apply
    assert "host-script-jobs.json" in apply
    assert "mac.openclaw_host_script_jobs.v1" in apply
    assert "writeFileSync" in apply
    assert "authorized_slack_channels" in apply
    # The guardrail is NOT regressed: script jobs still install disabled with
    # the honest description and deferred_script_jobs is still reported.
    assert "const enable = hasScript ? false : Boolean(job.enabled);" in apply
    assert "NOT yet ported to OpenClaw" in apply
    assert "deferred_script_jobs" in apply


def test_installer_schedules_script_jobs_via_launchd_and_systemd() -> None:
    installer = INSTALLER.read_text(encoding="utf-8")
    # The runner is installed to a host bin path (mode 0700) and invoked.
    assert "mac-cron-script-runner" in installer
    assert "run-script-cron-job.py" in installer
    assert 'chmod 0700 "$runner_dst"' in installer
    assert "install_host_script_runner" in installer
    # The installer consumes / emits the host-script-jobs spec.
    assert "host-script-jobs.json" in installer
    assert '"authorized_slack_channels"' in installer
    # BOTH supervisor branches schedule the host runner.
    assert "schedule_launchd_script_job" in installer
    assert "StartCalendarInterval" in installer
    assert "LaunchAgents" in installer
    assert "schedule_systemd_script_job" in installer
    assert "systemctl --user" in installer
    assert "OnCalendar=" in installer
    assert ".timer" in installer
    assert "Persistent=true" in installer
    assert "RunAtLoad" in installer
    assert "install_kslug_workspace_skill" in installer
    assert "workspace-skills/kslug-nightly-news/SKILL.md" in installer
    skill = OPENCLAW_DIR / "workspace-skills" / "kslug-nightly-news" / "SKILL.md"
    assert skill.is_file()
    body = skill.read_text(encoding="utf-8")
    assert ":tv: _KSLUG NIGHTLY NEWS_ :tv:" in body
    assert "Do not use Slack tools" in body


def test_runner_and_installer_are_syntactically_valid() -> None:
    import ast
    import subprocess

    ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    subprocess.run(["bash", "-n", str(INSTALLER)], check=True, timeout=30)


def test_a_multi_line_prompt_is_staged_in_the_sandbox_not_passed_as_argv(
    tmp_path, monkeypatch
) -> None:
    """The other half of the newline defect.

    openclaw-agent is the same sandbox wrapper as openclaw-message, so a
    multi-line prompt is refused identically -- and the refusal was returned AS
    the agent's reply. Once delivery was fixed, the fleet published that error
    to Slack hourly: a 138-character "dream report" that was the sandbox
    complaining about newlines.
    """
    wrapper = tmp_path / "openclaw-agent"
    wrapper.write_text(
        'OPEN_SHELL=/bin/openshell\nSANDBOX=mac-openclaw-test\nexec "$OPEN_SHELL" sandbox exec\n',
        encoding="utf-8",
    )
    seen = {}

    import subprocess as _sp

    def fake_run(args, **kwargs):
        seen["args"] = args
        seen["input"] = kwargs.get("input")
        return _sp.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)

    path = runner.stage_prompt_in_sandbox(str(wrapper), "one\ntwo", session_id="s1")

    assert path.startswith("/sandbox/prompts/")
    assert seen["input"] == "one\ntwo", "the body must travel on stdin, which is not argv"
    assert not any("\n" in str(part) for part in seen["args"]), (
        "an argv token still carries a newline, so the sandbox will refuse it"
    )


def test_staging_falls_back_rather_than_losing_the_run(tmp_path) -> None:
    """A path that is not a sandbox wrapper must degrade to the argv form, not
    raise: a non-sandboxed deployment still has to work."""
    plain = tmp_path / "not-a-wrapper"
    plain.write_text('#!/bin/sh\nexec openclaw agent "$@"\n', encoding="utf-8")

    assert runner.stage_prompt_in_sandbox(str(plain), "one\ntwo") == ""


# --------------------------------------------------------------------------- #
# Sandbox CLI mutex (task_2e7e9e31fda34902a288324792b4baeb)                    #
#                                                                              #
# dream-cycle and dream-synthesis are independent launchd jobs that share an   #
# hourly StartCalendarInterval; concurrent openclaw CLI invocations raced to   #
# open the sandbox's shared plugin-state SQLite DB and corrupted it            #
# ("database disk image is malformed"). Schedule staggering (apply-cron-      #
# plan.mjs) reduces contention but any future collision -- a new job, a       #
# manual run, clock drift -- reintroduces the race, so run_locked() is the    #
# durable guarantee: it serializes every subprocess call into the sandbox CLI  #
# regardless of why two of them landed at once.                                #
# --------------------------------------------------------------------------- #
def test_run_locked_serializes_concurrent_callers(tmp_path, monkeypatch) -> None:
    import threading
    import time as _time

    lock_path = tmp_path / "sandbox-cli.lock"
    intervals = []
    intervals_guard = threading.Lock()

    def fake_run(args, **kwargs):
        start = _time.monotonic()
        _time.sleep(0.05)
        end = _time.monotonic()
        with intervals_guard:
            intervals.append((start, end))
        return runner.subprocess.CompletedProcess(args, 0, "", "")

    real_subprocess_run = runner.subprocess.run
    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    try:
        threads = [
            threading.Thread(
                target=runner.run_locked, args=(["noop"],), kwargs={"lock_path": lock_path}
            )
            for _ in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
    finally:
        monkeypatch.setattr(runner.subprocess, "run", real_subprocess_run)

    assert len(intervals) == 4, "every caller must eventually acquire the lock and run"
    ordered = sorted(intervals)
    for (_, prev_end), (next_start, _) in zip(ordered, ordered[1:]):
        assert next_start >= prev_end, (
            "two sandbox CLI invocations overlapped -- the mutex did not serialize them"
        )


def test_run_locked_times_out_rather_than_hanging_forever(tmp_path, monkeypatch) -> None:
    import fcntl

    lock_path = tmp_path / "sandbox-cli.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with pytest.raises(TimeoutError):
            runner.run_locked(["noop"], lock_path=lock_path, lock_timeout=0.2)
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)


def test_default_agent_runner_serializes_through_the_sandbox_cli_lock(monkeypatch) -> None:
    calls = []

    def fake_run_locked(argv, **kwargs):
        calls.append(argv)
        return runner.subprocess.CompletedProcess(argv, 0, '{"text": "ok"}', "")

    monkeypatch.setattr(runner, "run_locked", fake_run_locked)
    output = runner.default_agent_runner("/bin/openclaw-agent", "hello")

    assert calls, "default_agent_runner must route through run_locked, not raw subprocess.run"
    assert output == '{"text": "ok"}'


def test_default_deliver_runner_serializes_through_the_sandbox_cli_lock(monkeypatch) -> None:
    calls = []

    def fake_run_locked(argv, **kwargs):
        calls.append(argv)
        return runner.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(runner, "run_locked", fake_run_locked)
    runner.default_deliver_runner("/bin/openclaw-message", ("slack", "C123"), "hello there")

    assert calls, "default_deliver_runner must route through run_locked, not raw subprocess.run"
    assert calls[0][0] == "/bin/openclaw-message"
