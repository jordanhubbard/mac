"""When the repository verifier fails, say what happened.

Three canary tasks failed with the identical line:

    OpenShell repository verifier did not start within 120.0s

Every one had run its work successfully first -- the agents did the job, and
the result was discarded at the gate. The message named a timeout and nothing
else, so the previous response was to raise the timeout (45s -> 120s), which
changed nothing, because the verifier was not slow.

Both output streams were captured all along. The failure path returned without
reading either.
"""

from __future__ import annotations

import io
import json

from mac.executor_sandbox import _verifier_output_excerpt


class _Stream(io.StringIO):
    pass


def test_stderr_is_reported():
    excerpt = _verifier_output_excerpt(_Stream(""), _Stream("openshell: no such image"))

    assert "openshell: no such image" in excerpt
    assert excerpt.startswith("stderr=")


def test_stdout_is_reported_when_stderr_is_empty():
    """A launcher that fails politely may say so on stdout."""
    excerpt = _verifier_output_excerpt(_Stream("could not resolve sandbox"), _Stream(""))

    assert "could not resolve sandbox" in excerpt


def test_silence_produces_no_noise():
    """Nothing to add is better than an empty `stderr=` suffix on every line."""
    assert _verifier_output_excerpt(_Stream(""), _Stream("   ")) == ""


def test_a_flood_is_truncated():
    """The message lands in task evidence and a diagnosis event; a megabyte of
    output there helps nobody and costs everybody."""
    excerpt = _verifier_output_excerpt(_Stream(""), _Stream("x" * 10000))

    assert len(excerpt) < 1000
    assert "truncated" in excerpt


def test_newlines_are_flattened():
    """Evidence is read one line at a time in `mac task show`."""
    excerpt = _verifier_output_excerpt(_Stream(""), _Stream("first\nsecond"))

    assert "\n" not in excerpt
    assert "first" in excerpt and "second" in excerpt


def test_a_broken_stream_does_not_raise():
    """Diagnostics run on the failure path; raising there would replace a
    reported failure with an unreported crash."""

    class Broken:
        def seek(self, _pos):
            raise OSError("closed")

        def read(self):
            raise OSError("closed")

    assert _verifier_output_excerpt(Broken(), Broken()) == ""


def test_both_spellings_are_retryable_infrastructure():
    """The failure is the same fault whichever way it is reported. Without
    this, making the message more accurate would reclassify a retryable
    infrastructure failure as a permanent one and burn the task's attempts."""
    from mac.services import _OPENSHELL_VERIFIER_INFRASTRUCTURE_MARKERS

    for message in (
        "OpenShell repository verifier did not start within 120.0s",
        "OpenShell repository verifier exited immediately (rc=1 after 0.3s, "
        "start budget 120.0s): stderr=openshell: no such image",
    ):
        assert any(
            marker in message.lower() for marker in _OPENSHELL_VERIFIER_INFRASTRUCTURE_MARKERS
        ), message


def test_a_failing_gate_reports_what_the_gate_said(monkeypatch):
    """A non-zero gate arrived as a bare exit status.

    The in-sandbox verifier writes the gate's stdout/stderr into
    mac-sandbox-verification.json and exits with the gate's status, printing
    nothing. So the host's excerpt of the two streams is empty and the detail
    degrades to "repository verifier exited with status 3" -- a number, for a
    run that produced a full pytest report. Five distinct causes were
    diagnosed by hand off that one message.
    """
    import json as _json
    import subprocess as _subprocess

    from mac import executor_sandbox

    report = {
        "returncode": 3,
        "status": "fail",
        "stdout": "FAILED tests/test_thing.py::test_case - AssertionError\n"
        "1 failed, 900 passed in 1200.00s",
        "stderr": "",
    }

    def fake_run(argv, **_kwargs):
        assert executor_sandbox._SANDBOX_VERIFICATION_FILE in argv
        return _subprocess.CompletedProcess(argv, 0, stdout=_json.dumps(report), stderr="")

    monkeypatch.setattr(executor_sandbox.subprocess, "run", fake_run)

    detail = executor_sandbox._sandbox_verification_report_detail("mac-task-abc", "/sandbox/task")

    assert "test_thing.py::test_case" in detail
    assert "1 failed, 900 passed" in detail


def test_failing_tests_survive_a_long_coverage_table(monkeypatch):
    """The gate prints its coverage table after pytest's summary. A tail-only
    excerpt kept the table and dropped the failing test names (2026-10-11,
    task_b9696e32), so the failures are pulled out and reported first."""
    import json as _json
    import subprocess as _subprocess

    from mac import executor_sandbox

    coverage = "\n".join(
        "src/mac/module_%03d.py   100   5   20   1  95.00%%   1-5" % i for i in range(400)
    )
    report = {
        "returncode": 1,
        "status": "fail",
        "stdout": "....F....\n"
        "[gw5] node down: Not properly terminated\n"
        "FAILED tests/test_alpha.py::test_one - AssertionError: boom\n"
        "FAILED tests/test_beta.py::test_two - TimeoutError\n"
        "==== 2 failed, 8950 passed, 3 skipped in 700.00s ====\n" + coverage,
        "stderr": "",
    }

    def fake_run(argv, **_kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout=_json.dumps(report), stderr="")

    monkeypatch.setattr(executor_sandbox.subprocess, "run", fake_run)
    detail = executor_sandbox._sandbox_verification_report_detail("s", "/sandbox/task")

    assert detail.startswith("failures=")
    assert "tests/test_alpha.py::test_one - AssertionError: boom" in detail
    assert "tests/test_beta.py::test_two" in detail
    assert "2 failed, 8950 passed" in detail
    assert "[gw5] node down" in detail
    assert "module_399.py" in detail  # the tail excerpt is still there


def test_the_sandbox_failure_list_is_used_when_stdout_was_clipped(monkeypatch):
    """The sandbox clips stdout to a head and a tail, and in a full-suite run
    both are coverage table: the FAILED lines between them never reached the
    host (task_14104f23, 2026-10-11). The sandbox reports them separately."""
    import json as _json
    import subprocess as _subprocess

    from mac import executor_sandbox

    report = {
        "returncode": 1,
        "status": "fail",
        "stdout": "src/mac/a.py  100  5  95%\n… [90000 chars omitted] …\nTOTAL  67697  8192  85.58%",
        "stderr": "",
        "failures": [
            "FAILED tests/test_alpha.py::test_one - AssertionError: boom",
            "==== 1 failed, 8955 passed in 600.00s ====",
        ],
    }

    def fake_run(argv, **_kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout=_json.dumps(report), stderr="")

    monkeypatch.setattr(executor_sandbox.subprocess, "run", fake_run)
    detail = executor_sandbox._sandbox_verification_report_detail("s", "/sandbox/task")

    assert detail.startswith("failures=FAILED tests/test_alpha.py::test_one")
    assert "1 failed, 8955 passed" in detail


def test_every_failure_name_survives_the_workers_diagnosis_clip(monkeypatch):
    """The worker keeps 1000 head + 3000 tail chars of its output, and the gate
    report sits near the end, before the agent's closing summary. A 1200-char
    coverage excerpt after the failures pushed four of seven names into the
    clipped middle (task_77031acb, 2026-10-11)."""
    import json as _json
    import subprocess as _subprocess

    from mac import executor_sandbox
    from mac.worker import _truncate_process_text

    # Sized like the live report: ~120-char node ids, and a closing block of
    # ~1000 chars (profile line, "I am editing ..." and the task summary).
    names = [
        "FAILED tests/test_selftest_report_executor_%d.py::"
        "test_host_loop_worker_serializes_obtainable_attestation_case_%d" % (i, i)
        for i in range(7)
    ]
    report = {
        "returncode": 1,
        "status": "fail",
        "stdout": "\n".join(
            "src/mac/module_%03d.py   100   5   20   1  95.00%%   1-5" % i for i in range(400)
        ),
        "stderr": "",
        "failures": names + ["= 7 failed, 8977 passed in 676.58s ="],
    }

    def fake_run(argv, **_kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout=_json.dumps(report), stderr="")

    monkeypatch.setattr(executor_sandbox.subprocess, "run", fake_run)
    detail = executor_sandbox._sandbox_verification_report_detail("s", "/sandbox/task")
    summary = (
        "[hub-verifier-profile] bounded-tmpfs ready\n"
        + "I am editing "
        + "y" * 300
        + "\n"
        + "=== MAC TASK SUMMARY ===\n"
        + "x" * 600
        + "\n=== END MAC TASK SUMMARY ==="
    )
    output = "agent log line\n" * 5000 + "[gate] phase tests: rc=1: " + detail + "\n" + summary

    tail = _truncate_process_text(output, 4000)

    for name in names:
        assert name.split(" ", 1)[1] in tail
    assert "7 failed, 8977 passed" in tail


def test_the_sandbox_and_host_agree_on_what_a_failure_line_is():
    from mac import executor_sandbox

    source = executor_sandbox._sandbox_repository_verification_shell({})
    assert 'r"%s"' % executor_sandbox._PYTEST_FAILURE_LINE.pattern in source


def test_the_sandbox_reports_failing_tests_that_clipping_would_drop(tmp_path):
    import os
    import subprocess as _subprocess
    import sys

    from mac import executor_sandbox

    workspace = tmp_path / "task"
    worktree = workspace / "repo"
    worktree.mkdir(parents=True)
    task_file = workspace / "task.json"
    task_file.write_text(
        json.dumps(
            {
                "task": {
                    "metadata": {
                        "repository_contract": {
                            "schema": "mac.repository_contract.v1",
                            "test": {"command": "sh gate.sh"},
                        }
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (worktree / "gate.sh").write_text(
        "\n".join(
            [
                'i=0; while [ $i -lt 200 ]; do echo "src/mac/head_$i.py 100 5 95%"; i=$((i+1)); done',
                "echo 'FAILED tests/test_alpha.py::test_one - AssertionError: boom'",
                "echo '==== 1 failed, 10 passed in 1.00s ===='",
                'i=0; while [ $i -lt 400 ]; do echo "src/mac/tail_$i.py 100 5 95%"; i=$((i+1)); done',
                "exit 1",
            ]
        ),
        encoding="utf-8",
    )

    _subprocess.run(
        ["bash", "-lc", executor_sandbox._sandbox_repository_verification_shell()],
        cwd=workspace,
        env={
            **os.environ,
            "MAC_TASK_WORKSPACE": str(workspace),
            "MAC_TASK_FILE": str(task_file),
            "MAC_TASK_REPO_WORKTREE": str(worktree),
            "MAC_SANDBOX_PYTHON": sys.executable,
        },
        capture_output=True,
        text=True,
        check=False,
    )

    manifest = json.loads((workspace / "mac-sandbox-verification.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "fail"
    assert "test_alpha.py" not in manifest["stdout"]  # clipped away
    assert manifest["failures"] == [
        "FAILED tests/test_alpha.py::test_one - AssertionError: boom",
        "==== 1 failed, 10 passed in 1.00s ====",
    ]


def test_an_unreadable_report_leaves_the_original_failure_intact(monkeypatch):
    """Recovery runs on the failure path. A sandbox that has already died, or
    a truncated report, must not turn a reported gate failure into a crash --
    the original exit status is still the answer."""
    import subprocess as _subprocess

    from mac import executor_sandbox

    def fake_run(argv, **_kwargs):
        return _subprocess.CompletedProcess(argv, 1, stdout="not json{", stderr="")

    monkeypatch.setattr(executor_sandbox.subprocess, "run", fake_run)
    assert executor_sandbox._sandbox_verification_report_detail("gone", "/sandbox/task") == ""

    def exploding_run(argv, **_kwargs):
        raise OSError("sandbox is gone")

    monkeypatch.setattr(executor_sandbox.subprocess, "run", exploding_run)
    assert executor_sandbox._sandbox_verification_report_detail("gone", "/sandbox/task") == ""


def test_a_failed_bootstrap_is_named_as_such(monkeypatch):
    """Bootstrap failure is reported as the run's exit status, so "the tests
    failed" is what the host says when dependency setup never finished and no
    test ran. The two lead opposite ways -- fix a regression, or repair an
    environment -- so the phase has to be named."""
    import json as _json
    import subprocess as _subprocess

    from mac import executor_sandbox

    report = {
        "returncode": 3,
        "status": "fail",
        "stdout": "",
        "stderr": "",
        "bootstrap": {
            "returncode": 3,
            "status": "fail",
            "stderr": "error: pg_config executable not found",
        },
    }

    def fake_run(argv, **_kwargs):
        return _subprocess.CompletedProcess(argv, 0, stdout=_json.dumps(report), stderr="")

    monkeypatch.setattr(executor_sandbox.subprocess, "run", fake_run)
    detail = executor_sandbox._sandbox_verification_report_detail("s", "/sandbox/task")

    assert "bootstrap failed (rc=3)" in detail
    assert "pg_config executable not found" in detail
