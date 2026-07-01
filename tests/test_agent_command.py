from __future__ import annotations

import json
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


def test_hermes_prompt_is_loaded_in_process_and_private_files_are_unlinked(
    tmp_path: Path, monkeypatch
) -> None:
    prompt = "private task instructions"
    command, prompt_file = _inputs(
        tmp_path,
        [
            "/opt/mac-venv/bin/python",
            "-m",
            "hermes_cli.main",
            "chat",
            "--query",
            agent_command.PROMPT_SENTINEL,
            "--yolo",
        ],
        prompt,
    )
    seen: dict[str, object] = {}

    def fake_run_module(name: str, *, run_name: str, alter_sys: bool) -> None:
        seen.update(name=name, argv=list(sys.argv), run_name=run_name, alter_sys=alter_sys)

    monkeypatch.setattr(agent_command.runpy, "run_module", fake_run_module)

    assert agent_command.main(
        ["--command-file", str(command), "--prompt-file", str(prompt_file)]
    ) == 0
    assert seen["name"] == "hermes_cli.main"
    assert prompt in seen["argv"]
    assert not command.exists()
    assert not prompt_file.exists()


def test_external_agent_receives_prompt_on_stdin_not_argv(
    tmp_path: Path, monkeypatch
) -> None:
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

    assert agent_command.main(
        ["--command-file", str(command), "--prompt-file", str(prompt_file)]
    ) == 0
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


def test_hermes_non_numeric_system_exit_is_failure(monkeypatch) -> None:
    monkeypatch.setattr(agent_command.sys, "argv", ["pytest"])

    def exit_with_message(*_args, **_kwargs):
        raise SystemExit("bad invocation")

    monkeypatch.setattr(agent_command.runpy, "run_module", exit_with_message)
    assert agent_command._run_hermes_in_process(
        [
            "python",
            "-m",
            "hermes_cli.main",
            "--query",
            agent_command.PROMPT_SENTINEL,
        ],
        "prompt",
    ) == 1


def test_codex_prompt_uses_stdin_marker(monkeypatch) -> None:
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(argv=list(argv), **kwargs)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(agent_command.subprocess, "run", fake_run)
    assert agent_command._run_external_with_stdin(
        ["codex", "exec", agent_command.PROMPT_SENTINEL], "private prompt"
    ) == 0
    assert seen["argv"] == ["codex", "exec", "-"]
    assert seen["input"] == "private prompt"
