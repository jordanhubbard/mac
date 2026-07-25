"""``MacAgentBackend`` -- the production :class:`~mac.acp.server.PromptBackend`.

This is the deferred Phase-2 follow-up from ADR 0006: a real backend so an
external ACP client driving a mac agent (via :class:`~mac.acp.server.ACPAgentServer`)
actually runs a mac agent turn instead of the :class:`~mac.acp.server.EchoBackend`
placeholder.

On :meth:`MacAgentBackend.run_prompt` it spawns the host's OpenClaw wrapper
for the prompt, in the session's ``cwd``, and streams the agent's stdout back to
the client line-by-line as ``agent_message_chunk`` ``session/update``
notifications so the client sees live progress.

The agent command is configurable via ``MAC_ACP_BACKEND_CMD`` (shlex-split). When
unset it invokes ``~/.mac/bin/openclaw-agent``. That wrapper enters the
verified long-lived OpenClaw OpenShell sandbox; ACP has no direct provider or
vendored-Hermes fallback.

A ``runner`` seam is injected for tests: ``runner(argv, cwd, on_line) -> int``
runs the command, invoking ``on_line(text)`` for each output line and returning
the process exit code. The default runner is a real subprocess; tests pass a
fake runner that calls ``on_line`` a couple of times and returns a code, keeping
the test suite subprocess-free.

Stop-reason mapping:

* cancellation observed (``turn.cancelled``) -> :data:`StopReason.CANCELLED`
  (the subprocess is terminated);
* exit code ``0`` -> :data:`StopReason.END_TURN`;
* any non-zero exit -> :data:`StopReason.REFUSAL` (with a final chunk noting the
  failure).
"""

from __future__ import annotations

import os
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Callable, List, Optional, Sequence

from mac import mac_paths

from .protocol import StopReason
from .server import PromptTurn, _prompt_text


__all__ = [
    "MacAgentBackend",
    "RunnerFn",
    "OnLineFn",
    "default_argv",
]


#: Callback a runner invokes for each line of agent output.
OnLineFn = Callable[[str], None]

#: A runner: run ``argv`` in ``cwd``, calling ``on_line`` per output line, and
#: return the process exit code. The default is :func:`_subprocess_runner`;
#: tests inject a fake to stay subprocess-free.
RunnerFn = Callable[[Sequence[str], str, OnLineFn], int]


# Environment knob: an explicit agent command line (shlex-split) overriding the
# derived default. Honored at run time so tests/operators can flip it freely.
_ENV_BACKEND_CMD = "MAC_ACP_BACKEND_CMD"


def default_argv(prompt: str) -> List[str]:
    """Build the OpenClaw-in-OpenShell invocation for ``prompt``."""

    wrapper = (
        os.environ.get("MAC_OPENCLAW_AGENT_BIN")
        or str(mac_paths.mac_home() / "bin" / "openclaw-agent")
    )
    return [
        wrapper,
        "--agent",
        "main",
        "--message",
        prompt,
        "--session-id",
        "mac-acp",
        "--json",
    ]


class MacAgentBackend:
    """A :class:`~mac.acp.server.PromptBackend` that runs a real mac agent turn.

    Parameters
    ----------
    argv:
        Optional explicit command template. When ``None`` (the default) the
        command is resolved per turn from ``$MAC_ACP_BACKEND_CMD`` (shlex-split)
        or, failing that, :func:`default_argv`. An explicit ``argv`` (or the env
        command) receives the prompt as a trailing positional argument; the
        derived default already embeds the prompt via ``--message``.
    runner:
        The execution seam: ``runner(argv, cwd, on_line) -> returncode``. Defaults
        to :func:`_subprocess_runner`. Tests inject a fake that calls
        ``on_line`` and returns a code -- no subprocess required.
    """

    def __init__(
        self,
        argv: Optional[Sequence[str]] = None,
        *,
        runner: Optional[RunnerFn] = None,
    ) -> None:
        self._argv = list(argv) if argv is not None else None
        self._runner = runner if runner is not None else _subprocess_runner

    # -- argv resolution -----------------------------------------------------

    def _resolve_argv(self, prompt: str) -> List[str]:
        """Build the command line for this turn's ``prompt``.

        Precedence: explicit ``argv`` (constructor) > ``$MAC_ACP_BACKEND_CMD`` >
        :func:`default_argv`. The first two are templates with the prompt
        appended as a trailing positional argument; the derived default already
        carries the prompt via ``--query``.
        """

        if self._argv is not None:
            return [*self._argv, prompt]
        env_cmd = (os.environ.get(_ENV_BACKEND_CMD) or "").strip()
        if env_cmd:
            return [*shlex.split(env_cmd), prompt]
        return default_argv(prompt)

    # -- the backend protocol ------------------------------------------------

    def run_prompt(self, turn: PromptTurn) -> str:
        """Run the agent for ``turn`` and stream its output back to the client.

        Already-cancelled turns short-circuit to :data:`StopReason.CANCELLED`
        before spawning anything. Output lines are streamed as
        ``agent_message_chunk`` updates as they arrive. We run on the server's
        per-turn worker thread, so blocking here is fine (see
        :meth:`mac.acp.server.ACPAgentServer._run_turn`).

        Cancellation is observed two ways: the ``on_line`` callback raises
        :class:`_Cancelled` if the turn is cancelled (so a chatty agent is
        killed promptly between lines), and the runner receives a
        ``cancelled`` predicate it polls so a *silent* agent is killed too. The
        default :func:`_subprocess_runner` honors both.
        """

        if turn.cancelled:
            return StopReason.CANCELLED

        prompt = _prompt_text(turn.content)
        argv = self._resolve_argv(prompt)
        cwd = turn.cwd or os.getcwd()

        def _on_line(text: str) -> None:
            if turn.cancelled:
                raise _Cancelled()
            if text:
                turn.agent_message_chunk(text)

        returncode = _invoke(self._runner, argv, cwd, _on_line, turn)

        if turn.cancelled:
            return StopReason.CANCELLED
        if returncode == 0:
            return StopReason.END_TURN
        turn.agent_message_chunk("[mac-agent exited with code %s]" % returncode)
        return StopReason.REFUSAL


class _Cancelled(Exception):
    """Sentinel raised by ``on_line`` to tell a runner to stop and kill its child."""


def _invoke(
    runner: RunnerFn,
    argv: Sequence[str],
    cwd: str,
    on_line: OnLineFn,
    turn: PromptTurn,
) -> int:
    """Call ``runner`` and absorb a cooperative :class:`_Cancelled` abort.

    A runner that finishes normally returns its exit code. A runner whose
    ``on_line`` raised :class:`_Cancelled` (the cancellation path) lets that
    propagate; we treat it as "cancelled, no exit code" and return a sentinel
    ``-1`` -- ``run_prompt`` ignores the code on the cancelled path anyway.
    """

    try:
        return runner(argv, cwd, on_line)
    except _Cancelled:
        return -1


def _subprocess_runner(argv: Sequence[str], cwd: str, on_line: OnLineFn) -> int:
    """The default :data:`RunnerFn`: spawn ``argv`` and stream its stdout.

    Reads the child's combined stdout line by line, handing each (newline
    stripped) to ``on_line``. If ``on_line`` raises :class:`_Cancelled` the child
    is terminated and the abort re-raised. A background watcher also terminates
    the child if cancellation is observed while the agent is producing no output
    (so a silent, hung agent is still killed). Always waits on the child so we
    report an accurate exit code and never leak a zombie.
    """

    proc = subprocess.Popen(  # noqa: S603 -- argv is operator/derived, not shell
        list(argv),
        cwd=cwd or None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,  # line-buffered
    )
    assert proc.stdout is not None  # PIPE guarantees a readable stream

    # Watcher: probe ``on_line("")`` periodically -- it raises _Cancelled once
    # the turn is cancelled (the empty string is never streamed). This kills a
    # silent child without waiting for it to emit a line.
    stop_watch = threading.Event()
    cancelled = threading.Event()

    def _watch() -> None:
        while not stop_watch.wait(0.02):
            try:
                on_line("")
            except _Cancelled:
                cancelled.set()
                _terminate(proc)
                return
            except Exception:  # noqa: BLE001 -- watcher must never crash the run
                return

    watcher = threading.Thread(target=_watch, daemon=True)
    watcher.start()

    aborted = False
    try:
        for raw in proc.stdout:
            line = raw.rstrip("\n")
            try:
                on_line(line)
            except _Cancelled:
                aborted = True
                _terminate(proc)
                break
    finally:
        stop_watch.set()
        try:
            proc.stdout.close()
        except Exception:  # noqa: BLE001 -- best-effort cleanup
            pass
        returncode = proc.wait()

    if aborted or cancelled.is_set():
        raise _Cancelled()
    return returncode


def _terminate(proc: "subprocess.Popen") -> None:
    """Terminate ``proc``, escalating to kill if it ignores SIGTERM."""

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - extreme edge
            pass
