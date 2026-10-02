"""Run a fleet agent without placing its task prompt in the OS process argv."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Sequence


PROMPT_SENTINEL = "__MAC_PROMPT_FROM_PRIVATE_FILE__"

# Once the in-process Hermes run has returned, everything left is interpreter
# shutdown: atexit cleanup plus joining non-daemon threads. A single wedged
# tool worker thread (e.g. a subagent that hit its timeout but whose
# run_conversation never unwound) used to hold the wrapper alive until the
# executor's agent timeout killed it — 900s of dead air after the work was
# already done, with the evidence manifest discarded as "incomplete". Bound
# the shutdown: give cleanup a grace window, then force the exit with the
# run's real return code.
DEFAULT_EXIT_GRACE_SECONDS = 60.0
EXIT_GRACE_ENV = "MAC_AGENT_COMMAND_EXIT_GRACE_SECONDS"


def _exit_grace_seconds() -> float:
    raw = (os.environ.get(EXIT_GRACE_ENV) or "").strip()
    if not raw:
        return DEFAULT_EXIT_GRACE_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_EXIT_GRACE_SECONDS


def _arm_shutdown_watchdog(returncode: int) -> None:
    """Force process exit if interpreter shutdown outlives the grace window."""
    grace = _exit_grace_seconds()
    if grace <= 0:
        return

    def _force_exit() -> None:
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except Exception:  # noqa: BLE001 - nothing may block the forced exit
                pass
        os._exit(returncode)

    watchdog = threading.Timer(grace, _force_exit)
    watchdog.daemon = True
    watchdog.start()


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


def _run_external_with_stdin(argv: list[str], prompt: str) -> int:
    # The CLI consumes the prompt on stdin: appending prompt text to argv would
    # expose it for the lifetime of the child process.
    del argv[argv.index(PROMPT_SENTINEL)]
    completed = subprocess.run(argv, input=prompt, text=True, check=False)
    return int(completed.returncode)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the agent command wrapper entry point and return its exit code."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--command-file", required=True)
    parser.add_argument("--prompt-file", required=True)
    args = parser.parse_args(argv)
    command, prompt = _read_private_inputs(Path(args.command_file), Path(args.prompt_file))
    # The `python -m hermes_cli.main` in-process branch was removed with the
    # vendored Hermes tree on 2026-08-17. Every coding agent now runs through
    # the external path, which keeps the prompt off the command line by feeding
    # it on stdin.
    return _run_external_with_stdin(command, prompt)


if __name__ == "__main__":
    # Arm the watchdog ONLY in the real wrapper process. Library/test callers
    # of main() must not inherit a delayed os._exit() in their interpreter.
    _rc = main()
    _arm_shutdown_watchdog(_rc)
    raise SystemExit(_rc)
