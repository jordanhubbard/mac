"""The verifier must not inherit the agent's stdin.

THE BUG THIS CLOSES

Every task the fleet ran completed its work and then had it discarded, with:

    OpenShell repository verifier did not start within 120.0s

`openshell sandbox exec` READS STDIN. The agent runs under a supervisor whose
stdin is an open pipe that never delivers, and the verifier was launched
without redirecting stdin, so it inherited that pipe and blocked before running
anything: no stdout, no stderr, no marker file, and a start-timeout reported for
a process that was never going to start.

Measured on a worker: with stdin an open pipe the exec hangs until killed
(rc=124, zero output); with stdin on /dev/null the identical command returns in
one second.

The lesson generalises past this one call, which is why every launch site is
covered here rather than just the verifier: a subprocess that might read stdin
and is given a stdin nobody will ever write to is a subprocess that hangs.
"""

from __future__ import annotations

import subprocess

import pytest

from mac import executor_prompt, executor_sandbox


class _Recorder:
    def __init__(self):
        self.kwargs = {}

    def __call__(self, argv, **kwargs):
        self.kwargs = kwargs
        raise RuntimeError("stop here: the launch is what is under test")


def test_the_repository_verifier_launches_with_stdin_closed(monkeypatch, tmp_path):
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)

    executor_sandbox._sandbox_run_repository_verification_exec(
        "sandbox-name",
        "/workspace/repo",
        "/workspace/repo/verify.sh",
        "/tmp/marker",
        timeout=5.0,
    )

    assert recorder.kwargs.get("stdin") is subprocess.DEVNULL


def test_the_shared_capture_helper_closes_stdin(monkeypatch, tmp_path):
    """Every openshell lifecycle step -- upload, download, delete -- goes
    through here, so an inherited stdin is the same hang with a different
    symptom."""
    recorder = _Recorder()
    monkeypatch.setattr(subprocess, "Popen", recorder)

    with pytest.raises(RuntimeError):
        executor_prompt._run_captured(["openshell", "sandbox", "list"], tmp_path, 5.0)

    assert recorder.kwargs.get("stdin") is subprocess.DEVNULL


def test_no_openshell_launch_inherits_stdin():
    """A source-level guard, because the failure is invisible in review: the
    call looks complete, stdout and stderr are both handled, and the only thing
    missing is the stream nobody thinks about."""
    import ast
    import pathlib

    root = pathlib.Path(executor_sandbox.__file__).parent
    offenders = []
    for path in (root / "executor_sandbox.py", root / "executor_prompt.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = getattr(node.func, "attr", "")
            if target not in {"Popen", "run"}:
                continue
            names = {kw.arg for kw in node.keywords}
            # Only launches that capture output are process launches we own;
            # anything else is a helper call with a matching name.
            if not ({"stdout", "stderr", "capture_output"} & names):
                continue
            if "stdin" not in names:
                offenders.append("%s:%d" % (path.name, node.lineno))

    assert not offenders, (
        "these subprocess launches inherit the agent's stdin and will hang if "
        "the child reads it: %s" % ", ".join(offenders)
    )
