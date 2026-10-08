"""The Hermes question gate: the plugin's decisions and its installer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from mac import hermes_question_gate as gate
from mac.hermes_runtime import (
    render_fleet_section,
    refresh_fleet_section,
    write_runtime_context,
)
from mac.worker import _QUESTION_CODE_ANSWER, _question_slack_text

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_DIR = ROOT / "src" / "mac" / "data" / "hermes_plugins" / gate.PLUGIN_NAME


def _plugin() -> Any:
    spec = importlib.util.spec_from_file_location("mac_question_gate_plugin", PLUGIN_DIR / "__init__.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


plugin = _plugin()


def _event(
    text: str,
    *,
    platform: str = "slack",
    thread_ts: Optional[str] = None,
    context: Optional[str] = None,
    raw_text: Optional[str] = None,
) -> SimpleNamespace:
    ts = "1791479615.216699"
    raw = {"ts": ts, "text": raw_text if raw_text is not None else text}
    if thread_ts:
        raw["thread_ts"] = thread_ts
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(platform=SimpleNamespace(value=platform)),
        raw_message=raw,
        reply_to_message_id=thread_ts,
        channel_context=context,
    )


def _question_parent_context() -> str:
    """Thread context as the Hermes Slack adapter renders it under a MAC question."""
    posted = _question_slack_text(
        {"title": "Answer needed: Chat reply test", "body": "What is your favourite colour?"}, "Q7"
    )
    return (
        "[Thread context — prior messages in this thread (not yet in conversation history):]\n"
        "[thread parent] Rocky: %s\n[End of thread context]\n\n" % " ".join(posted.split())
    )


# --- the plugin -------------------------------------------------------------


@pytest.mark.parametrize("text", ["Q7 blue", "q7: blue", "*Q7* - blue", "Q12 — wombats"])
def test_a_coded_answer_in_the_channel_is_dropped(text: str) -> None:
    assert plugin.decide(_event(text)) == plugin.SKIP_CODE


@pytest.mark.parametrize("text", ["Quick question", "Q7", "what about Q7 blue?", "Qx blue"])
def test_other_channel_messages_go_through(text: str) -> None:
    assert plugin.decide(_event(text)) is None


def test_the_plugin_drops_exactly_what_the_worker_relays() -> None:
    samples = ["Q7 blue", "q7: blue", "*Q7* - blue", "Q7", "Quick question", "what about Q7 blue?", "Q7\nblue"]
    for text in samples:
        relayed = bool(_QUESTION_CODE_ANSWER.match(text))
        assert (plugin.decide(_event(text)) is not None) == relayed, text


def test_a_reply_in_a_question_thread_is_dropped() -> None:
    event = _event("green", thread_ts="1791473794.337529", context=_question_parent_context())
    assert plugin.decide(event) == plugin.SKIP_THREAD


def test_a_reply_in_any_other_thread_goes_through() -> None:
    context = "[thread parent] jkh: what should we build next?\n"
    assert plugin.decide(_event("Q7 blue", thread_ts="1791473794.337529", context=context)) is None
    # An agent already in the thread gets no context: it keeps talking.
    assert plugin.decide(_event("green", thread_ts="1791473794.337529", context=None)) is None


def test_a_message_that_mentions_someone_goes_through() -> None:
    assert plugin.decide(_event("Q7 blue", raw_text="<@U0AKBJ0A0VA> Q7 blue")) is None
    threaded = _event(
        "can you check this?",
        raw_text="<@U0AKM0ZUDKK> can you check this?",
        thread_ts="1791473794.337529",
        context=_question_parent_context(),
    )
    assert plugin.decide(threaded) is None


def test_other_platforms_are_left_alone() -> None:
    assert plugin.decide(_event("Q7 blue", platform="telegram")) is None


def test_the_hook_never_raises_and_registers_itself() -> None:
    assert plugin._pre_gateway_dispatch(event=object(), gateway=None, session_store=None) is None
    hooks = {}
    plugin.register(SimpleNamespace(register_hook=lambda name, fn: hooks.setdefault(name, fn)))
    assert list(hooks) == ["pre_gateway_dispatch"]


# --- the installer ----------------------------------------------------------


def _home(tmp_path: Path, config: str = "model: x\n") -> Path:
    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(config, encoding="utf-8")
    return home


def test_the_plugin_is_installed_once_and_a_foreign_copy_is_left_alone(tmp_path: Path) -> None:
    home = _home(tmp_path)
    assert gate.ensure_plugin(home) == "installed"
    assert gate.ensure_plugin(home) == "unchanged"
    target = home / "plugins" / gate.PLUGIN_NAME
    assert (target / "plugin.yaml").read_text() == (PLUGIN_DIR / "plugin.yaml").read_text()
    (target / "__init__.py").write_text("# old managed copy\n# Managed by mac.hermes_question_gate\n")
    assert gate.ensure_plugin(home) == "updated"
    (target / "__init__.py").write_text("# someone else's plugin\n")
    assert gate.ensure_plugin(home).startswith("skipped")


@pytest.mark.parametrize(
    "config, expected",
    [
        ("model: x\n", "model: x\nplugins:\n  enabled:\n    - mac-question-gate\n"),
        (
            "plugins:\n  disabled: []\nmodel: x\n",
            "plugins:\n  enabled:\n    - mac-question-gate\n  disabled: []\nmodel: x\n",
        ),
        (
            "plugins:\n  enabled:\n  - disk-cleanup\nmodel: x\n",
            "plugins:\n  enabled:\n  - disk-cleanup\n  - mac-question-gate\nmodel: x\n",
        ),
        (
            "plugins:\n  enabled: [disk-cleanup]\n",
            "plugins:\n  enabled:\n    - disk-cleanup\n    - mac-question-gate\n",
        ),
    ],
)
def test_the_plugin_is_added_to_plugins_enabled(tmp_path: Path, config: str, expected: str) -> None:
    home = _home(tmp_path, config)
    assert gate.ensure_enabled(home / "config.yaml") == "added"
    assert (home / "config.yaml").read_text() == expected
    assert gate.ensure_enabled(home / "config.yaml") == "unchanged"
    assert list(home.glob("config.yaml.bak-*-pre-question-gate"))


def test_a_plugin_disabled_by_hand_stays_disabled(tmp_path: Path) -> None:
    config = "plugins:\n  disabled:\n    - mac-question-gate\n"
    home = _home(tmp_path, config)
    assert gate.ensure_enabled(home / "config.yaml") == "disabled"
    assert (home / "config.yaml").read_text() == config


def _write_context(home: Path, tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    write_runtime_context(
        context_path=home / "mac-runtime-context.json",
        markdown_path=home / "mac-runtime-context.md",
        hermes_env_path=home / ".env",
        agent_name="rocky",
        fleet_name="mac",
        mac_url="http://127.0.0.1:8789",
        hermes_home=home,
        mac_home=tmp_path / "mac",
        workspace_path=workspace,
    )
    with (home / ".env").open("a", encoding="utf-8") as env:
        env.write("AGENT=rocky\nMAC_HUB_URL=http://127.0.0.1:8789\nMAC_HOME=%s\n" % (tmp_path / "mac"))


def test_the_runtime_context_is_refreshed_and_keeps_its_fleet_block(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _write_context(home, tmp_path)
    assert gate.refresh_runtime_context(home) == "unchanged"

    stale = json.loads((home / "mac-runtime-context.json").read_text())
    stale["runtime_rules"] = ["an old rule"]
    (home / "mac-runtime-context.json").write_text(json.dumps(stale))
    fleet = render_fleet_section({"generated_at": "now", "members": [{"name": "natasha"}]})
    refresh_fleet_section(home / "mac-runtime-context.md", fleet)

    assert gate.refresh_runtime_context(home) == "refreshed"
    refreshed = json.loads((home / "mac-runtime-context.json").read_text())
    assert refreshed["runtime_rules"] != ["an old rule"]
    markdown = (home / "mac-runtime-context.md").read_text()
    assert "is recorded by MAC itself" in markdown
    assert fleet in markdown


def test_a_refresh_that_would_change_identity_writes_nothing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    _write_context(home, tmp_path)
    with (home / ".env").open("a", encoding="utf-8") as env:
        env.write("AGENT=someone-else\nMAC_AGENT_ID=agent_someone\n")
    before = (home / "mac-runtime-context.json").read_text()
    assert gate.refresh_runtime_context(home).startswith("skipped: refreshing would change")
    assert (home / "mac-runtime-context.json").read_text() == before


def test_install_reports_each_step_and_skips_a_host_without_hermes(tmp_path: Path, capsys) -> None:
    assert gate.install(tmp_path / "none")["status"] == "skipped"
    home = _home(tmp_path)
    assert gate.main(["--hermes-home", str(home)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {
        "enabled": "added",
        "plugin": "installed",
        "runtime_context": "skipped: no runtime context at %s" % (home / "mac-runtime-context.json"),
        "status": "ok",
    }


def test_fleet_update_runs_the_installer_on_the_hub_and_the_workers() -> None:
    script = (ROOT / "scripts" / "fleet-update").read_text()
    assert '"$VENV/bin/python" -m mac.hermes_question_gate --hermes-home' in script
    assert '"$venv/bin/python" -m mac.hermes_question_gate --hermes-home "$hh"' in script
