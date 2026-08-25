from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

import pytest

from mac import agent_command


def _inputs(tmp_path: Path, argv: list[str], prompt: str) -> tuple[Path, Path]:
    command = tmp_path / "command.json"
    prompt_file = tmp_path / "prompt.txt"
    command.write_text(json.dumps({"argv": argv}), encoding="utf-8")
    prompt_file.write_text(prompt, encoding="utf-8")
    return command, prompt_file


def test_external_agent_receives_prompt_on_stdin_not_argv(tmp_path: Path, monkeypatch) -> None:
    prompt = "secret-free process list"
    command, prompt_file = _inputs(
        tmp_path,
        ["claude", "--dangerously-skip-permissions", "-p", agent_command.PROMPT_SENTINEL],
        prompt,
    )
    seen: dict[str, object] = {}

    class _Completed:
        returncode = 0

    def fake_run(argv, *, input, text, check):
        seen.update(argv=list(argv), input=input, text=text, check=check)
        return _Completed()

    monkeypatch.setattr(agent_command.subprocess, "run", fake_run)

    assert (
        agent_command.main(["--command-file", str(command), "--prompt-file", str(prompt_file)]) == 0
    )
    assert prompt not in seen["argv"]
    assert seen["input"] == prompt
    assert not command.exists()
    assert not prompt_file.exists()


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "non-empty string argv"),
        (["claude", "--print"], "exactly one prompt sentinel"),
    ],
)
def test_private_inputs_reject_malformed_commands_and_unlink(
    tmp_path: Path, argv: list[str], message: str
) -> None:
    command, prompt_file = _inputs(tmp_path, argv, "private")

    with pytest.raises(ValueError, match=message):
        agent_command._read_private_inputs(command, prompt_file)

    assert not command.exists()
    assert not prompt_file.exists()


def test_codex_prompt_uses_stdin_marker(monkeypatch) -> None:
    for name in ("MAC_CODEX_TOKEN", "CODEX_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=list(argv), **kwargs)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(agent_command.subprocess, "run", fake_run)
    assert (
        agent_command._run_external_with_stdin(
            ["codex", "exec", agent_command.PROMPT_SENTINEL], "private prompt"
        )
        == 0
    )
    assert seen["argv"] == ["codex", "exec", "-"]
    assert seen["input"] == "private prompt"
    assert "env" not in seen


def test_codex_bearer_route_uses_ephemeral_home(monkeypatch, tmp_path: Path) -> None:
    stale_home = tmp_path / "copied-codex-home"
    stale_home.mkdir()
    (stale_home / "auth.json").write_text(
        '{"auth_mode":"chatgpt","tokens":{"refresh_token":"stale"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENAI_API_KEY", "router-token")
    monkeypatch.setenv("CODEX_HOME", str(stale_home))
    seen: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        child_env = kwargs["env"]
        isolated_home = Path(child_env["CODEX_HOME"])
        seen.update(
            argv=list(argv),
            input=kwargs["input"],
            isolated_home=isolated_home,
            isolated_home_existed=isolated_home.is_dir(),
        )
        assert isolated_home != stale_home
        assert not (isolated_home / "auth.json").exists()
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(agent_command.subprocess, "run", fake_run)

    assert (
        agent_command._run_external_with_stdin(
            ["codex", "exec", agent_command.PROMPT_SENTINEL], "private prompt"
        )
        == 0
    )
    assert seen["argv"] == ["codex", "exec", "-"]
    assert seen["input"] == "private prompt"
    assert seen["isolated_home_existed"] is True
    assert not Path(seen["isolated_home"]).exists()
    assert (stale_home / "auth.json").exists()


def test_the_shutdown_watchdog_is_armed_only_by_the_real_wrapper(tmp_path: Path) -> None:
    """Importing the module must never arm a delayed forced exit.

    The watchdog exists because a stuck non-daemon thread used to hold the
    wrapper alive until the executor's 900s agent timeout killed it. It is
    armed under ``__main__`` and ONLY there: an in-process caller -- this test
    suite included -- must not inherit an os._exit() timer.

    The companion case that drove a foreign module in-process went away with
    the vendored Hermes runtime on 2026-08-17; `python -m hermes_cli.main` was
    the only in-process branch, and every coding agent now takes the external
    stdin path. This pins the half of the contract that still holds.
    """
    import threading

    before = {t for t in threading.enumerate()}
    importlib.reload(agent_command)
    after = {t for t in threading.enumerate()}

    new_timers = [t for t in after - before if isinstance(t, threading.Timer)]
    assert not new_timers, "importing agent_command armed a watchdog timer"


def test_the_watchdog_is_disabled_when_the_grace_window_is_zero() -> None:
    """A zero/absent grace window must arm nothing at all."""
    import threading

    os.environ["MAC_AGENT_COMMAND_EXIT_GRACE_SECONDS"] = "0"
    try:
        before = {t for t in threading.enumerate()}
        agent_command._arm_shutdown_watchdog(0)
        after = {t for t in threading.enumerate()}
        assert not [t for t in after - before if isinstance(t, threading.Timer)]
    finally:
        os.environ.pop("MAC_AGENT_COMMAND_EXIT_GRACE_SECONDS", None)
