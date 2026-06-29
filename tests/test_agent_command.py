from __future__ import annotations

import json
import sys
from pathlib import Path

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
