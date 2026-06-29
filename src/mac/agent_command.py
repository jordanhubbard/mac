"""Run a fleet agent without placing its task prompt in the OS process argv."""

from __future__ import annotations

import argparse
import json
import runpy
import subprocess
import sys
from pathlib import Path
from typing import Sequence


PROMPT_SENTINEL = "__MAC_PROMPT_FROM_PRIVATE_FILE__"


def _read_private_inputs(command_file: Path, prompt_file: Path) -> tuple[list[str], str]:
    try:
        loaded = json.loads(command_file.read_text(encoding="utf-8"))
        argv = loaded.get("argv") if isinstance(loaded, dict) else None
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            raise ValueError("agent command file must contain a non-empty string argv")
        prompt = prompt_file.read_text(encoding="utf-8")
        if argv.count(PROMPT_SENTINEL) != 1:
            raise ValueError("agent command must contain exactly one prompt sentinel")
        return list(argv), prompt
    finally:
        # Inputs are single-use. Unlink before the long-running agent starts so
        # credentials/task text do not linger in a copied sandbox workspace.
        command_file.unlink(missing_ok=True)
        prompt_file.unlink(missing_ok=True)


def _is_hermes_module(argv: Sequence[str]) -> bool:
    return len(argv) >= 4 and argv[1:3] == ["-m", "hermes_cli.main"]


def _run_hermes_in_process(argv: list[str], prompt: str) -> int:
    # The wrapper already runs under the selected Hermes interpreter. Running
    # the module in-process keeps the real prompt out of /proc/<pid>/cmdline.
    sys.argv = ["hermes_cli.main", *argv[3:]]
    sys.argv[sys.argv.index(PROMPT_SENTINEL)] = prompt
    try:
        runpy.run_module("hermes_cli.main", run_name="__main__", alter_sys=False)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, (int, type(None))) else 1
    return 0


def _run_external_with_stdin(argv: list[str], prompt: str) -> int:
    sentinel_index = argv.index(PROMPT_SENTINEL)
    executable = Path(argv[0]).name.lower()
    if executable in {"codex", "codex.exe"} or (
        len(argv) > 1 and argv[1] == "exec"
    ):
        argv[sentinel_index] = "-"
    else:
        # Claude, Cursor, and explicit command overrides consume the prompt on
        # stdin. This is the only supported override contract because appending
        # prompt text to argv exposes it for the lifetime of the child process.
        del argv[sentinel_index]
    completed = subprocess.run(argv, input=prompt, text=True, check=False)
    return int(completed.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-file", required=True)
    parser.add_argument("--prompt-file", required=True)
    args = parser.parse_args(argv)
    command, prompt = _read_private_inputs(
        Path(args.command_file), Path(args.prompt_file)
    )
    if _is_hermes_module(command):
        return _run_hermes_in_process(command, prompt)
    return _run_external_with_stdin(command, prompt)


if __name__ == "__main__":
    raise SystemExit(main())
