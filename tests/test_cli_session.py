"""CLI session auto-join to the AgentBus (ADR 0032 auto-trigger addendum).

The claim under test: a `mac` invocation from inside a detected coding-CLI
harness registers a durable identity and wires that harness's hook config,
with no separate `mac admin plugin install` step -- and does all of that
without ever blocking or failing the command the user actually ran.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest

from mac import cli_session
from mac.services import ControlPlane


@pytest.fixture(autouse=True)
def _isolate_cli_session_state(tmp_path, monkeypatch):
    """Every test in this file runs against scratch cache and config paths.

    ``ensure_registered_cached``/``auto_join`` persist a small marker under
    ``~/.mac/cli-session/`` on the real machine by default. Without this
    fixture, test state survives into the next test and can escape onto the
    developer's or CI runner's real home directory.
    """
    monkeypatch.setattr(
        cli_session, "_cache_path", lambda agent_id: tmp_path / (agent_id + ".json")
    )
    monkeypatch.setattr(
        cli_session,
        "_claude_settings_path",
        lambda user_home=None: (user_home or tmp_path) / ".claude" / "settings.json",
    )


@pytest.fixture()
def cp():
    return ControlPlane.in_memory()


def test_detect_live_harness_recognizes_claude_code():
    assert cli_session.detect_live_harness({"CLAUDECODE": "1"}) == "claude"


def test_detect_live_harness_is_none_for_a_plain_terminal():
    assert cli_session.detect_live_harness({"PATH": "/usr/bin", "TERM": "xterm"}) is None


def test_session_identity_is_deterministic_and_host_user_scoped():
    a = cli_session.session_identity("laptop.local", "jordanh")
    b = cli_session.session_identity("laptop.local", "jordanh")
    other_user = cli_session.session_identity("laptop.local", "someoneelse")
    other_host = cli_session.session_identity("other.local", "jordanh")

    assert a == b
    assert a["agent_id"] != other_user["agent_id"]
    assert a["agent_id"] != other_host["agent_id"]
    # The machine identity is host-scoped only, so two users on the same
    # laptop share one machine row rather than each registering a duplicate.
    assert (
        a["machine_id"] == cli_session.session_identity("laptop.local", "someoneelse")["machine_id"]
    )


def test_ensure_registered_creates_a_live_agentbus_participant(cp):
    identity = cli_session.ensure_registered(
        cp, harness="claude", hostname="laptop.local", user="jordanh"
    )

    agent = cp.get_agent(identity["agent_id"])
    assert agent.id == identity["agent_id"]
    assert "cli_session" in agent.capabilities
    assert "harness:claude" in agent.capabilities

    # And it is a genuinely live bus participant: sending it a message and
    # draining its inbox works without a NotFoundError, which is exactly the
    # failure mode an unregistered ad-hoc agent_id hits today.
    cp.register_machine(hostname="sender.local", machine_id="machine_sender")
    cp.register_agent(machine_id="machine_sender", name="sender", agent_id="agent_sender")
    stream = cp.open_agentbus_stream(
        sender_agent_id="agent_sender", recipient_agent_id=identity["agent_id"]
    )
    cp.append_agentbus_chunk(stream.id, "agent_sender", payload={"text": "hello"})
    drained = cp.drain_agentbus_inbox(identity["agent_id"])
    assert drained["count"] == 1


def test_ensure_registered_is_idempotent(cp):
    first = cli_session.ensure_registered(cp, harness="claude", hostname="h", user="u")
    second = cli_session.ensure_registered(cp, harness="claude", hostname="h", user="u")

    assert first["agent_id"] == second["agent_id"]
    agents = cp.list_agents() if hasattr(cp, "list_agents") else None
    if agents is not None:
        matching = [a for a in agents if a.id == first["agent_id"]]
        assert len(matching) == 1


def test_ensure_registered_cached_skips_the_second_call_within_ttl(cp, tmp_path, monkeypatch):
    monkeypatch.setattr(
        cli_session, "_cache_path", lambda agent_id: tmp_path / (agent_id + ".json")
    )
    calls = []
    real = cli_session.ensure_registered

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(cli_session, "ensure_registered", counting)

    cli_session.ensure_registered_cached(
        cp, harness="claude", hostname="h", user="u", ttl_seconds=3600
    )
    cli_session.ensure_registered_cached(
        cp, harness="claude", hostname="h", user="u", ttl_seconds=3600
    )

    assert len(calls) == 1


def test_auto_join_is_a_silent_noop_with_no_detected_harness(cp):
    assert cli_session.auto_join(cp, environ={"TERM": "xterm"}) is None


def test_auto_join_never_raises_when_the_plane_is_broken():
    class BrokenPlane:
        def register_machine(self, *a, **k):
            raise RuntimeError("hub unreachable")

    # Must not raise -- ADR 0032 sec 5: failure must not take down the session.
    assert cli_session.auto_join(BrokenPlane(), environ={"CLAUDECODE": "1"}) is None


def test_merge_claude_hooks_is_idempotent_and_preserves_existing_hooks(tmp_path):
    document_path = tmp_path / "settings.json"
    document_path.write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo hi"}]}]}}
        ),
        encoding="utf-8",
    )

    first_write = cli_session._merge_claude_hooks(document_path)
    assert first_write is True
    second_write = cli_session._merge_claude_hooks(document_path)
    assert second_write is False

    document = json.loads(document_path.read_text(encoding="utf-8"))
    session_start = document["hooks"]["SessionStart"]
    # The pre-existing hook survives untouched.
    assert any(
        h.get("command") == "echo hi" for entry in session_start for h in entry.get("hooks", [])
    )
    # And mac's own fail-open hook was added alongside it, not in place of it.
    command = next(
        h["command"]
        for entry in session_start
        for h in entry.get("hooks", [])
        if "cli-session hook" in h.get("command", "")
    )
    assert command == cli_session._hook_command("SessionStart")
    assert "||" in command
    assert len(session_start) == 2


def test_merge_claude_hooks_upgrades_the_blocking_legacy_command(tmp_path):
    document_path = tmp_path / "settings.json"
    document_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"hooks": [{"type": "command", "command": "mac admin cli-session hook"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert cli_session._merge_claude_hooks(document_path) is True
    document = json.loads(document_path.read_text(encoding="utf-8"))
    configured = document["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert configured == cli_session._hook_command("UserPromptSubmit")


def test_generated_hook_fails_open_when_mac_command_is_missing(tmp_path):
    result = subprocess.run(
        cli_session._hook_command("UserPromptSubmit"),
        shell=True,
        capture_output=True,
        text=True,
        env={"PATH": str(tmp_path)},
        check=False,
    )

    assert result.returncode == 0
    assert json.loads(result.stdout) == {"continue": True}


def test_hook_command_availability_ignores_unreleased_pythonpath(monkeypatch):
    observed = {}

    def fake_run(argv, **kwargs):
        observed["argv"] = argv
        observed["env"] = kwargs["env"]
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(cli_session.shutil, "which", lambda command, path=None: "/bin/mac")
    monkeypatch.setattr(cli_session.subprocess, "run", fake_run)

    assert cli_session._hook_command_available(
        {"PATH": "/bin", "PYTHONPATH": "/tmp/unreleased/src"}
    )
    assert observed["argv"] == ["/bin/mac", "admin", "cli-session", "hook", "--help"]
    assert "PYTHONPATH" not in observed["env"]


def test_auto_join_does_not_publish_hook_before_command_is_installed(cp, tmp_path, monkeypatch):
    monkeypatch.setattr(cli_session, "_hook_command_available", lambda environ=None: False)

    identity = cli_session.auto_join(
        cp,
        environ={"CLAUDECODE": "1", "USER": "test-user"},
    )

    assert identity is not None
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_install_hook_config_writes_under_the_given_user_home(tmp_path):
    changed = cli_session.install_hook_config("claude", user_home=tmp_path)
    assert changed is True
    written = tmp_path / ".claude" / "settings.json"
    assert written.is_file()
    document = json.loads(written.read_text(encoding="utf-8"))
    assert "SessionStart" in document["hooks"]


def test_install_hook_config_is_a_noop_for_an_unimplemented_harness(tmp_path):
    assert cli_session.install_hook_config("cursor", user_home=tmp_path) is False
    assert not (tmp_path / ".cursor").exists()


def test_render_claude_hook_output_empty_inbox_is_valid_not_an_error():
    output = cli_session.render_claude_hook_output([])
    assert output["hookSpecificOutput"]["additionalContext"] == ""


def test_render_claude_hook_output_names_the_actual_event():
    output = cli_session.render_claude_hook_output([], event="SessionStart")
    assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_render_claude_hook_output_lists_pending_messages():
    output = cli_session.render_claude_hook_output(
        [{"sender_agent_id": "agent_rocky", "payload": {"text": "check the queue"}}]
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "agent_rocky" in context
    assert "check the queue" in context


def test_run_hook_drains_and_renders_without_raising_on_a_broken_plane():
    class BrokenPlane:
        def drain_agentbus_inbox(self, *a, **k):
            raise RuntimeError("hub unreachable")

    output = cli_session.run_hook(BrokenPlane(), harness="claude")
    assert output["hookSpecificOutput"]["additionalContext"] == ""
