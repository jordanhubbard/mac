"""Tests for the dream-cycle RESTORATION mechanism (task_c8bb46ec).

Covers the host-side two-stage runner (deploy/openclaw/run-script-cron-job.py),
the host-runner spec emission in apply-cron-plan.mjs, and the host-side
scheduling wired into install-openclaw-gateway.sh.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

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


def test_resolve_delivery_target_decision() -> None:
    slack_origin = {"origin": {"platform": "slack", "chat_id": "C0123ABC"}}
    assert runner.resolve_delivery_target(slack_origin) == ("slack", "C0123ABC")
    assert runner.resolve_delivery_target({"delivery": "slack:G9ZZ99Z"}) == ("slack", "G9ZZ99Z")
    # Explicit local, or unaddressed, => no delivery target.
    assert runner.resolve_delivery_target({"delivery": "local"}) is None
    assert runner.resolve_delivery_target({}) is None
    # A human name OpenClaw cannot resolve durably => local, never wrong channel.
    assert runner.resolve_delivery_target({"delivery": "slack:@jordan"}) is None


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


def test_run_job_missing_script_still_runs_and_writes_local(tmp_path: Path) -> None:
    """No script + no deliverable target: proceed with the unavailable note and
    persist the reply locally rather than firing a phantom reference."""
    seen = {}

    def fake_agent(agent_bin, prompt, *, session_id="", **kwargs):
        seen["prompt"] = prompt
        return json.dumps({"response": "logged locally"})

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
    assert "## Script Output" in seen["prompt"]
    assert "unavailable" in seen["prompt"]
    local_path = Path(result["local_path"])
    assert local_path.is_file()
    assert "logged locally" in local_path.read_text(encoding="utf-8")


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
    # BOTH supervisor branches schedule the host runner.
    assert "schedule_launchd_script_job" in installer
    assert "StartCalendarInterval" in installer
    assert "LaunchAgents" in installer
    assert "schedule_systemd_script_job" in installer
    assert "systemctl --user" in installer
    assert "OnCalendar=" in installer
    assert ".timer" in installer


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
    assert runner.sandbox_wrapper_settings(str(plain)) == ("", "")
