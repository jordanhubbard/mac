"""Launchd lifecycle coverage for standalone shared-service installers."""

from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import stat
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
LAUNCHD_LIFECYCLE = ROOT / "deploy" / "lib" / "launchd-lifecycle.sh"


@pytest.mark.parametrize(
    ("installer_name", "label", "health_assignment", "wrapper_call"),
    (
        (
            "install-qdrant-service.sh",
            "com.${FLEET_NAME}.qdrant",
            'health_url="${service_url}/collections"',
            "write_qdrant_wrapper",
        ),
        (
            "install-firecrawl-gateway.sh",
            "com.${FLEET_NAME}.firecrawl-gateway",
            'health_url="${service_url}/health"',
            "write_gateway_wrapper",
        ),
        (
            "install-webdav-server.sh",
            "com.${FLEET_NAME}.webdav",
            'health_url="http://${WEBDAV_BIND_ADDR}:${WEBDAV_PORT}/health"',
            "write_webdav_wrapper",
        ),
        (
            "install-headscale.sh",
            "com.${FLEET_NAME}.headscale",
            "# -- Wait for headscale to become ready --",
            "",
        ),
    ),
)
def test_shared_service_stops_before_plist_replacement_and_loads_before_health(
    installer_name: str,
    label: str,
    health_assignment: str,
    wrapper_call: str,
) -> None:
    script = (ROOT / "deploy" / installer_name).read_text(encoding="utf-8")

    source_path = script.index(
        '$(CDPATH= cd -P -- "$(dirname "${BASH_SOURCE[0]}")" && pwd)'
    )
    launchd_branch = script.index("  launchd)")
    source_helper = script.index("launchd-lifecycle.sh")
    stage_plist = script.index('cat > "$tmp_plist" <<EOF', launchd_branch)
    transaction = script.index("mac_launchd_transaction_begin", launchd_branch)
    stop = script.index(
        "mac_launchd_stop_job_if_present",
        launchd_branch,
    )
    replace_plist = script.index(
        'mac_launchd_transaction_replace "$tmp_plist" "$plist"', stop
    )
    bootstrap = script.index("mac_launchd_bootstrap_job", replace_plist)
    health = script.index(health_assignment, bootstrap)
    commit = script.index("mac_launchd_transaction_commit", health)

    assert label in script[launchd_branch:stage_plist]
    assert source_path < source_helper < stage_plist
    assert transaction < stage_plist < stop < replace_plist < bootstrap < health < commit
    if wrapper_call:
        wrapper_stage = script.index(f'{wrapper_call} "$tmp_wrapper"', transaction)
        wrapper_track = script.index(
            'mac_launchd_transaction_track_file "$wrapper"', transaction
        )
        wrapper_replace = script.index(
            'mac_launchd_transaction_replace "$tmp_wrapper" "$wrapper"', stop
        )
        assert transaction < wrapper_track < wrapper_stage < stop < wrapper_replace
    if installer_name == "install-qdrant-service.sh":
        container_stop = script.index("stop_qdrant_container_if_present", stop)
        assert stop < container_stop < replace_plist
    assert "launchctl bootout" not in script
    assert "launchctl bootstrap" not in script
    assert "launchctl kickstart" not in script


def _run_qdrant_container_stop(
    tmp_path: Path, mode: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    script = (ROOT / "deploy" / "install-qdrant-service.sh").read_text(
        encoding="utf-8"
    )
    functions = LAUNCHD_LIFECYCLE.read_text(encoding="utf-8") + "\n"
    functions += "qdrant_container_is_present() {" + script.split(
        "qdrant_container_is_present() {", 1
    )[1].split('case "$SUPERVISOR_KIND" in', 1)[0]
    case_dir = tmp_path / mode
    case_dir.mkdir()
    calls = case_dir / "calls"
    removed = case_dir / "removed"
    runtime = case_dir / "selected-runtime"
    stale_runtime = case_dir / "stale-runtime"
    runtime_script = """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_CONTAINER_CALLS"
case "$1" in
  ps)
    case "$FAKE_CONTAINER_MODE" in
      absent) exit 0 ;;
      inspect-error) echo 'synthetic daemon failure' >&2; exit 70 ;;
      cross-runtime)
        if [ "$(basename "$0")" = selected-runtime ]; then
          exit 0
        fi
        if [ -f "$FAKE_CONTAINER_REMOVED" ]; then
          exit 0
        fi
        echo mac-qdrant
        exit 0
        ;;
      present|remove-failed|persistent)
        if [ "$FAKE_CONTAINER_MODE" != persistent ] && [ -f "$FAKE_CONTAINER_REMOVED" ]; then
          exit 0
        fi
        echo mac-qdrant
        exit 0
        ;;
    esac
    ;;
  rm)
    if [ "$FAKE_CONTAINER_MODE" = remove-failed ]; then
      echo 'synthetic remove failure' >&2
      exit 9
    fi
    : > "$FAKE_CONTAINER_REMOVED"
    exit 0
    ;;
esac
exit 64
"""
    runtime.write_text(runtime_script, encoding="utf-8")
    runtime.chmod(0o755)
    if mode == "cross-runtime":
        stale_runtime.write_text(runtime_script, encoding="utf-8")
        stale_runtime.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            "set -euo pipefail\n"
            + functions
            + '\nCONTAINER_RUNTIME_PATHS=("$CONTAINER_CMD_ABS")\n'
            + "CONTAINER_RUNTIME_PATH_COUNT=1\n"
            + 'if [ -n "${SECOND_CONTAINER_RUNTIME:-}" ]; then '
            + 'CONTAINER_RUNTIME_PATHS[1]="$SECOND_CONTAINER_RUNTIME"; '
            + 'CONTAINER_RUNTIME_PATH_COUNT=2; fi\n'
            + "stop_qdrant_container_if_present",
        ],
        env={
            **os.environ,
            "CONTAINER_CMD_ABS": str(runtime),
            "SECOND_CONTAINER_RUNTIME": (
                str(stale_runtime) if mode == "cross-runtime" else ""
            ),
            "QDRANT_CONTAINER_NAME": "mac-qdrant",
            "FAKE_CONTAINER_MODE": mode,
            "FAKE_CONTAINER_CALLS": str(calls),
            "FAKE_CONTAINER_REMOVED": str(removed),
            "MAC_QDRANT_RUNTIME_COMMAND_TIMEOUT_SECONDS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    recorded = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return result, recorded


@pytest.mark.parametrize(
    ("mode", "succeeds", "expected_verbs", "error"),
    (
        ("absent", True, ["ps"], ""),
        ("present", True, ["ps", "rm", "ps"], ""),
        ("inspect-error", False, ["ps"], "could not inspect"),
        ("remove-failed", False, ["ps", "rm"], "could not retire"),
        ("persistent", False, ["ps", "rm", "ps"], "remained after removal"),
        ("cross-runtime", True, ["ps", "ps", "rm", "ps"], ""),
    ),
)
def test_qdrant_launchd_stop_retires_daemon_owned_container(
    tmp_path: Path,
    mode: str,
    succeeds: bool,
    expected_verbs: list[str],
    error: str,
) -> None:
    result, calls = _run_qdrant_container_stop(tmp_path, mode)

    assert (result.returncode == 0) is succeeds, result.stderr
    assert [call.split()[0] for call in calls] == expected_verbs
    if error:
        assert error in result.stderr


def _run_launchd_bootstrap(
    tmp_path: Path,
    mode: str,
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    case_dir = tmp_path / mode
    fake_bin = case_dir / "bin"
    fake_bin.mkdir(parents=True)
    calls = case_dir / "calls"
    print_count = case_dir / "print-count"
    print_count.write_text("0\n", encoding="utf-8")
    plist = case_dir / "service.plist"
    plist.write_text("synthetic plist\n", encoding="utf-8")

    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_LAUNCHCTL_CALLS"
case "$1" in
  print)
    value=$(sed -n '1p' "$FAKE_LAUNCHCTL_PRINT_COUNT")
    value=$((value + 1))
    printf '%s\n' "$value" > "$FAKE_LAUNCHCTL_PRINT_COUNT"
    case "$FAKE_LAUNCHCTL_MODE:$value" in
      inspect-error:*|post-inspect-error:2)
        echo 'synthetic launchctl transport failure' >&2
        exit 70
        ;;
      success:1|enable-failed:1|bootstrap-failed:1|final-absent:*|post-inspect-error:1)
        echo 'Could not find service synthetic' >&2
        exit 113
        ;;
      success:2|preloaded:*) exit 0 ;;
    esac
    exit 64
    ;;
  enable)
    if [ "$FAKE_LAUNCHCTL_MODE" = enable-failed ]; then
      echo 'synthetic enable refusal' >&2
      exit 8
    fi
    exit 0
    ;;
  bootstrap)
    if [ "$FAKE_LAUNCHCTL_MODE" = bootstrap-failed ]; then
      echo 'synthetic bootstrap refusal' >&2
      exit 9
    fi
    exit 0
    ;;
esac
exit 64
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail\n. "$1"\n'
            "mac_launchd_bootstrap_job "
            '"gui/501" "$2" "gui/501/com.mac.synthetic" "com.mac.synthetic"',
            "bash",
            str(LAUNCHD_LIFECYCLE),
            str(plist),
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_LAUNCHCTL_MODE": mode,
            "FAKE_LAUNCHCTL_CALLS": str(calls),
            "FAKE_LAUNCHCTL_PRINT_COUNT": str(print_count),
        },
        check=False,
        capture_output=True,
        text=True,
    )
    return result, calls.read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize(
    ("mode", "succeeds", "expected_calls", "error"),
    (
        (
            "success",
            True,
            [
                "print gui/501/com.mac.synthetic",
                "enable gui/501/com.mac.synthetic",
                "bootstrap gui/501 service.plist",
                "print gui/501/com.mac.synthetic",
            ],
            "",
        ),
        (
            "preloaded",
            False,
            ["print gui/501/com.mac.synthetic"],
            "prior generation is absent",
        ),
        (
            "enable-failed",
            False,
            [
                "print gui/501/com.mac.synthetic",
                "enable gui/501/com.mac.synthetic",
            ],
            "synthetic enable refusal",
        ),
        (
            "inspect-error",
            False,
            ["print gui/501/com.mac.synthetic"],
            "could not inspect launchd job",
        ),
        (
            "bootstrap-failed",
            False,
            [
                "print gui/501/com.mac.synthetic",
                "enable gui/501/com.mac.synthetic",
                "bootstrap gui/501 service.plist",
            ],
            "launchctl bootstrap failed",
        ),
        (
            "final-absent",
            False,
            [
                "print gui/501/com.mac.synthetic",
                "enable gui/501/com.mac.synthetic",
                "bootstrap gui/501 service.plist",
                "print gui/501/com.mac.synthetic",
            ],
            "not loaded after bootstrap",
        ),
        (
            "post-inspect-error",
            False,
            [
                "print gui/501/com.mac.synthetic",
                "enable gui/501/com.mac.synthetic",
                "bootstrap gui/501 service.plist",
                "print gui/501/com.mac.synthetic",
            ],
            "could not inspect launchd job",
        ),
    ),
)
def test_launchd_bootstrap_requires_absence_and_proves_new_job_loaded(
    tmp_path: Path,
    mode: str,
    succeeds: bool,
    expected_calls: list[str],
    error: str,
) -> None:
    result, calls = _run_launchd_bootstrap(tmp_path, mode)

    assert (result.returncode == 0) is succeeds, result.stderr
    normalized_calls = []
    for call in calls:
        parts = call.split()
        if parts[0] == "bootstrap":
            parts[2] = Path(parts[2]).name
        normalized_calls.append(" ".join(parts))
    assert normalized_calls == expected_calls
    if error:
        assert error in result.stderr


def test_bounded_runner_kills_entire_process_group(tmp_path: Path) -> None:
    worker = tmp_path / "ignore-term"
    child_pid = tmp_path / "child-pid"
    worker.write_text(
        """#!/bin/sh
set -eu
trap '' TERM
(trap '' TERM; while :; do sleep 5; done) &
printf '%s\n' "$!" > "$CHILD_PID_FILE"
while :; do sleep 5; done
""",
        encoding="utf-8",
    )
    worker.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail; . "$1"; mac_run_bounded 0.2 "$2"',
            "bash",
            str(LAUNCHD_LIFECYCLE),
            str(worker),
        ],
        env={**os.environ, "CHILD_PID_FILE": str(child_pid)},
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 124, result.stderr
    assert "bounded command timed out" in result.stderr
    pid = int(child_pid.read_text(encoding="utf-8"))
    process_gone = False
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            process_gone = True
            break
        time.sleep(0.05)
    assert process_gone, f"bounded command left child process {pid} alive"


def test_bounded_runner_preserves_stdout_and_stderr(tmp_path: Path) -> None:
    command = tmp_path / "two-streams"
    command.write_text(
        "#!/bin/sh\nprintf 'structured-output\\n'\nprintf 'diagnostic\\n' >&2\n",
        encoding="utf-8",
    )
    command.chmod(0o755)

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail; . "$1"; mac_run_bounded 1 "$2"',
            "bash",
            str(LAUNCHD_LIFECYCLE),
            str(command),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 0
    assert result.stdout == "structured-output\n"
    assert result.stderr == "diagnostic\n"


def test_retry_does_not_accept_timeout_handler_exit_zero(tmp_path: Path) -> None:
    command = tmp_path / "exit-zero-on-term"
    command.write_text(
        "#!/bin/sh\ntrap 'exit 0' TERM\nwhile :; do sleep 5; done\n",
        encoding="utf-8",
    )
    command.chmod(0o755)

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail; . "$1"; mac_retry_bounded 0.4 0.1 0.01 "$2"',
            "bash",
            str(LAUNCHD_LIFECYCLE),
            str(command),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode == 124, result.stderr
    assert "bounded retry deadline expired" in result.stderr


def _write_escaped_pipe_holder(path: Path) -> None:
    path.write_text(
        """#!/bin/sh
set -eu
"$PYTHON_FOR_CHILD" - "$ESCAPED_CHILD_READY" <<'PY' &
import os
from pathlib import Path
import signal
import sys
import time

os.setsid()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text("ready\n", encoding="utf-8")
while True:
    time.sleep(5)
PY
child=$!
while [ ! -s "$ESCAPED_CHILD_READY" ]; do sleep 0.01; done
printf '%s\n' "$child" > "$ESCAPED_CHILD_PID"
trap '' TERM
while :; do sleep 5; done
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _kill_recorded_process(path: Path) -> None:
    if not path.exists():
        return
    pid = int(path.read_text(encoding="utf-8"))
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


@pytest.mark.parametrize(
    "runner",
    (
        'mac_run_bounded 0.15 "$2"',
        'mac_retry_bounded 0.3 0.10 0.01 "$2"',
    ),
)
def test_bounded_helpers_do_not_wait_for_escaped_descendant_pipe_eof(
    tmp_path: Path, runner: str
) -> None:
    worker = tmp_path / "escaped-pipe-holder"
    child_pid = tmp_path / "escaped-child.pid"
    child_ready = tmp_path / "escaped-child.ready"
    _write_escaped_pipe_holder(worker)
    try:
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                f'set -euo pipefail; . "$1"; {runner}',
                "bash",
                str(LAUNCHD_LIFECYCLE),
                str(worker),
            ],
            env={
                **os.environ,
                "ESCAPED_CHILD_PID": str(child_pid),
                "ESCAPED_CHILD_READY": str(child_ready),
                "PYTHON_FOR_CHILD": sys.executable,
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
    finally:
        _kill_recorded_process(child_pid)

    assert result.returncode == 124, result.stderr


def test_launchd_wait_does_not_wait_for_escaped_descendant_pipe_eof(
    tmp_path: Path,
) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    child_pid = tmp_path / "escaped-child.pid"
    child_ready = tmp_path / "escaped-child.ready"
    _write_escaped_pipe_holder(launchctl)
    try:
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'set -euo pipefail; . "$1"; '
                'mac_launchd_wait_unloaded "gui/501/com.mac.synthetic" '
                '"com.mac.synthetic"',
                "bash",
                str(LAUNCHD_LIFECYCLE),
            ],
            env={
                **os.environ,
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "ESCAPED_CHILD_PID": str(child_pid),
                "ESCAPED_CHILD_READY": str(child_ready),
                "PYTHON_FOR_CHILD": sys.executable,
                "MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS": "0.10",
                "MAC_LAUNCHD_TRANSITION_TIMEOUT_SECONDS": "0.40",
                "MAC_LAUNCHD_POLL_INTERVAL_SECONDS": "0.01",
            },
            check=False,
            capture_output=True,
            text=True,
            timeout=4,
        )
    finally:
        _kill_recorded_process(child_pid)

    assert result.returncode == 2, result.stderr
    assert "timed out inspecting launchd job" in result.stderr


@pytest.mark.parametrize(
    ("runner", "diagnostic"),
    (
        ('mac_run_bounded 2 "$2"', "bounded command output exceeded"),
        (
            'mac_retry_bounded 2 1 0.01 "$2"',
            "bounded retry output exceeded",
        ),
    ),
)
def test_bounded_helpers_cap_and_reject_unlimited_output(
    tmp_path: Path, runner: str, diagnostic: str
) -> None:
    flood = tmp_path / "output-flood"
    flood.write_text(
        f"""#!{sys.executable}
import os

chunk = b"x" * (64 * 1024)
while True:
    os.write(1, chunk)
    os.write(2, chunk)
""",
        encoding="utf-8",
    )
    flood.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            f'set -euo pipefail; . "$1"; {runner}',
            "bash",
            str(LAUNCHD_LIFECYCLE),
            str(flood),
        ],
        env={**os.environ, "MAC_LAUNCHD_MAX_OUTPUT_BYTES": "4096"},
        check=False,
        capture_output=True,
        timeout=4,
    )

    assert result.returncode == 125, result.stderr.decode(errors="replace")
    assert len(result.stdout) <= 4096
    retained_stderr, marker, _message = result.stderr.partition(diagnostic.encode())
    assert marker
    assert len(retained_stderr) <= 4096


def test_launchd_wait_caps_and_rejects_unlimited_output(tmp_path: Path) -> None:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        f"""#!{sys.executable}
import os

chunk = b"x" * (64 * 1024)
while True:
    os.write(1, chunk)
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail; . "$1"; '
            'mac_launchd_wait_unloaded "gui/501/com.mac.synthetic" '
            '"com.mac.synthetic"',
            "bash",
            str(LAUNCHD_LIFECYCLE),
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "MAC_LAUNCHD_MAX_OUTPUT_BYTES": "4096",
            "MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS": "1",
            "MAC_LAUNCHD_TRANSITION_TIMEOUT_SECONDS": "2",
            "MAC_LAUNCHD_POLL_INTERVAL_SECONDS": "0.01",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=4,
    )

    assert result.returncode == 2, result.stderr
    assert "launchd inspection output exceeded 4096 bytes" in result.stderr


def test_lifecycle_bounded_helpers_never_use_communicate() -> None:
    lifecycle = LAUNCHD_LIFECYCLE.read_text(encoding="utf-8")

    assert ".communicate(" not in lifecycle


def _large_sparse_regular_file(path: Path, size: int = 64 * 1024 * 1024) -> int:
    with path.open("wb") as stream:
        stream.truncate(size)
    path.chmod(0o600)
    return size


def _start_artifact_helper(
    function_call: str, *arguments: Path
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [
            "/bin/bash",
            "-c",
            f'set -euo pipefail; . "$1"; {function_call}',
            "bash",
            str(LAUNCHD_LIFECYCLE),
            *(str(argument) for argument in arguments),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _wait_for_copy_artifact(
    process: subprocess.Popen[str], directory: Path, pattern: str
) -> Path:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        candidates = list(directory.glob(pattern))
        if candidates:
            assert process.poll() is None, "artifact copy finished before mutation boundary"
            return candidates[0]
        if process.poll() is not None:
            stdout, stderr = process.communicate(timeout=1)
            raise AssertionError(
                "artifact helper exited before exposing its copy boundary: "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(0.001)
    process.kill()
    process.wait(timeout=1)
    raise AssertionError("artifact helper did not begin its fd-backed copy")


def test_snapshot_rejects_in_place_mutation_after_source_open(tmp_path: Path) -> None:
    source = tmp_path / "source.plist"
    backup = tmp_path / "backup.plist"
    size = _large_sparse_regular_file(source)
    process = _start_artifact_helper(
        'mac_launchd_snapshot_file "$2" "$3" user', source, backup
    )
    _wait_for_copy_artifact(process, tmp_path, backup.name)

    with source.open("r+b", buffering=0) as stream:
        stream.seek(size // 2)
        stream.write(b"concurrent mutation")
        os.fsync(stream.fileno())
    stdout, stderr = process.communicate(timeout=8)

    assert process.returncode != 0, stdout
    assert "changed while snapshotting" in stderr
    assert not backup.exists()


def test_replace_rejects_source_rename_and_substitution_after_open(
    tmp_path: Path,
) -> None:
    source = tmp_path / ".service.plist.new"
    retained_source = tmp_path / "opened-source.plist"
    destination = tmp_path / "service.plist"
    _large_sparse_regular_file(source)
    destination.write_text("old generation\n", encoding="utf-8")
    process = _start_artifact_helper(
        'mac_launchd_atomic_replace "$2" "$3" user', source, destination
    )
    _wait_for_copy_artifact(process, tmp_path, f".{destination.name}.stage.*")

    source.rename(retained_source)
    source.write_text("substituted staged artifact\n", encoding="utf-8")
    stdout, stderr = process.communicate(timeout=8)

    assert process.returncode != 0, stdout
    assert "source lifecycle artifact changed while copying" in stderr
    assert destination.read_text(encoding="utf-8") == "old generation\n"
    assert source.read_text(encoding="utf-8") == "substituted staged artifact\n"
    assert retained_source.exists()
    assert not list(tmp_path.glob(f".{destination.name}.stage.*"))


@pytest.mark.parametrize("helper", ("snapshot", "replace"))
def test_artifact_helpers_reject_symlink_sources(
    tmp_path: Path, helper: str
) -> None:
    real_source = tmp_path / "real-source"
    source = tmp_path / "linked-source"
    destination = tmp_path / "destination"
    real_source.write_text("untrusted bytes\n", encoding="utf-8")
    source.symlink_to(real_source)
    if helper == "snapshot":
        call = 'mac_launchd_snapshot_file "$2" "$3" user'
    else:
        call = 'mac_launchd_atomic_replace "$2" "$3" user'
        destination.write_text("old generation\n", encoding="utf-8")

    process = _start_artifact_helper(call, source, destination)
    stdout, stderr = process.communicate(timeout=5)

    assert process.returncode != 0, stdout
    assert "symlink" in stderr or "not a regular file" in stderr
    if helper == "snapshot":
        assert not destination.exists()
    else:
        assert destination.read_text(encoding="utf-8") == "old generation\n"


def test_artifact_copy_contract_checks_fd_identity_and_readback() -> None:
    lifecycle = LAUNCHD_LIFECYCLE.read_text(encoding="utf-8")

    assert lifecycle.count('flags |= os.O_NOFOLLOW') >= 2
    assert lifecycle.count("identity(initial) != identity(opened)") >= 2
    assert "lifecycle artifact snapshot read-back mismatch" in lifecycle
    assert "lifecycle replacement read-back mismatch" in lifecycle


def _run_privileged_job_state(
    tmp_path: Path, sudo_mode: str
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    case_dir = tmp_path / f"sudo-{sudo_mode}"
    fake_bin = case_dir / "bin"
    fake_bin.mkdir(parents=True)
    calls = case_dir / "sudo-calls"
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_SUDO_CALLS"
case "$FAKE_SUDO_MODE" in
  failure)
    echo 'synthetic sudo refusal' >&2
    exit 77
    ;;
  timeout)
    trap '' TERM
    while :; do sleep 5; done
    ;;
esac
[ "$1" = -n ]
shift
exec "$@"
""",
        encoding="utf-8",
    )
    sudo.chmod(0o755)
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
set -eu
[ "$1" = print ]
echo 'Could not find service synthetic' >&2
exit 113
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -Eeuo pipefail; '
            'trap \'status=$?; printf "STRUCTURAL:%s\\n" "$status" >&2; '
            'exit "$status"\' ERR; '
            '. "$1"; '
            'mac_launchd_job_state "system/com.mac.synthetic" '
            '"com.mac.synthetic" system',
            "bash",
            str(LAUNCHD_LIFECYCLE),
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_SUDO_CALLS": str(calls),
            "FAKE_SUDO_MODE": sudo_mode,
            "MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS": "0.2",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    recorded = calls.read_text(encoding="utf-8").splitlines()
    return result, recorded


@pytest.mark.parametrize(
    ("sudo_mode", "succeeds", "error"),
    (
        ("success", True, ""),
        ("failure", False, "synthetic sudo refusal"),
        ("timeout", False, "timed out inspecting launchd job"),
    ),
)
def test_system_job_state_uses_exact_bounded_sudo_argv(
    tmp_path: Path,
    sudo_mode: str,
    succeeds: bool,
    error: str,
) -> None:
    result, calls = _run_privileged_job_state(tmp_path, sudo_mode)

    assert (result.returncode == 0) is succeeds, result.stderr
    assert ("STRUCTURAL:" not in result.stderr) is succeeds
    assert calls == ["-n launchctl print system/com.mac.synthetic"]
    if succeeds:
        assert result.stdout == "inactive\n"
    else:
        assert error in result.stderr


def test_privileged_mode_never_evaluates_a_command_prefix() -> None:
    lifecycle = LAUNCHD_LIFECYCLE.read_text(encoding="utf-8")

    assert "sudo -n launchctl" in lifecycle
    assert "sudo -n \"$python_bin\" -c" in lifecycle
    assert "shell=True" not in lifecycle
    assert "eval " not in lifecycle


def test_qdrant_empty_runtime_set_is_bash_32_nounset_safe() -> None:
    script = (ROOT / "deploy" / "install-qdrant-service.sh").read_text(
        encoding="utf-8"
    )
    stop_function = "stop_qdrant_container_if_present() {" + script.split(
        "stop_qdrant_container_if_present() {", 1
    )[1].split('case "$SUPERVISOR_KIND" in', 1)[0]
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            "set -euo pipefail\n"
            + "CONTAINER_RUNTIME_PATHS=()\n"
            + "CONTAINER_RUNTIME_PATH_COUNT=0\n"
            + stop_function
            + "\nstop_qdrant_container_if_present",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _run_launchd_transaction(
    tmp_path: Path,
    mode: str,
    execution_mode: str = "user",
    recovery_policy: str = "rollback",
) -> subprocess.CompletedProcess[str]:
    case_dir = tmp_path / mode
    fake_bin = case_dir / "bin"
    fake_bin.mkdir(parents=True)
    state = case_dir / "state"
    calls = case_dir / "calls"
    sudo_control_calls = case_dir / "sudo-control-calls"
    sudo_artifact_calls = case_dir / "sudo-artifact-calls"
    plist = case_dir / "service.plist"
    wrapper = case_dir / "service-run"
    staged_plist = case_dir / ".service.plist.new"
    staged_wrapper = case_dir / ".service-run.new"
    caller_cleanup = case_dir / "caller-cleanup"
    if mode.startswith("fresh-"):
        state.write_text("inactive\n", encoding="utf-8")
    else:
        initial_state = "inactive\n" if mode.startswith("prequiesced-") else "active\n"
        state.write_text(initial_state, encoding="utf-8")
        plist.write_text("old plist\n", encoding="utf-8")
        wrapper.write_text("old wrapper\n", encoding="utf-8")
        plist.chmod(0o640)
        wrapper.chmod(0o750)
    staged_plist.write_text("new plist\n", encoding="utf-8")
    staged_wrapper.write_text("new wrapper\n", encoding="utf-8")
    staged_plist.chmod(0o644)
    staged_wrapper.chmod(0o700)

    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
set -eu
printf '%s\n' "$*" >> "$FAKE_TX_CALLS"
case "$1" in
  print)
    if [ "$(sed -n '1p' "$FAKE_TX_STATE")" = active ]; then
      exit 0
    fi
    echo 'Could not find service synthetic' >&2
    exit 113
    ;;
  bootout)
    printf '%s\n' inactive > "$FAKE_TX_STATE"
    exit 0
    ;;
  enable) exit 0 ;;
  bootstrap)
    if grep -q '^new plist$' "$3" && [ "$FAKE_TX_MODE" = bootstrap-fail ]; then
      echo 'synthetic new bootstrap failure' >&2
      exit 9
    fi
    printf '%s\n' active > "$FAKE_TX_STATE"
    exit 0
    ;;
esac
exit 64
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)
    sudo = fake_bin / "sudo"
    sudo.write_text(
        """#!/bin/sh
set -eu
[ "$1" = -n ]
if [ "${2:-}" = launchctl ]; then
  printf '%s\n' "$*" >> "$FAKE_SUDO_CONTROL_CALLS"
else
  printf '%s|%s|%s\n' "$1" "$2" "$3" >> "$FAKE_SUDO_ARTIFACT_CALLS"
fi
shift
exec "$@"
""",
        encoding="utf-8",
    )
    sudo.chmod(0o755)

    command = """set -euo pipefail
. "$1"
# Transaction semantics are independent of the bounded-runner implementation.
# Exercise the real launchd and artifact code against the fake executables, but
# do not let scheduler latency turn this rollback test into a timeout test.
# Dedicated tests above cover the bounded runner and its process-group cleanup.
mac_launchd_run_control_bounded() {
  local mode="$1"
  shift 2
  case "$mode" in
    user) launchctl "$@" ;;
    system) sudo -n launchctl "$@" ;;
    *) return 2 ;;
  esac
}
mac_launchd_run_python_bounded() {
  local mode="$1" program="$3" python_bin=""
  shift 3
  python_bin="$(mac_launchd_python_bin)" || return $?
  case "$mode" in
    user) "$python_bin" -c "$program" "$@" ;;
    system) sudo -n "$python_bin" -c "$program" "$@" ;;
    *) return 2 ;;
  esac
}
CALLER_CLEANUP_PATH="$8"
deployment_exit_handler_equivalent() {
  local original_rc="$1"
  printf "cleanup:%s\\n" "$original_rc" >> "$CALLER_CLEANUP_PATH"
}
trap 'deployment_exit_handler_equivalent "$?"' EXIT
mac_launchd_transaction_begin "gui/501" "$2" "gui/501/com.mac.tx" "com.mac.tx" "$7"
mac_launchd_transaction_track_file "$3"
mac_launchd_transaction_track_temporary "$4"
mac_launchd_transaction_track_temporary "$5"
if [ "$6" = prequiesced-health-fail ]; then
  mac_launchd_transaction_set_expected_prior_state active
fi
mac_launchd_transaction_mark_mutating
mac_launchd_stop_job_if_present "gui/501/com.mac.tx" "com.mac.tx" "$7"
mac_launchd_transaction_replace "$4" "$3" 0700
mac_launchd_transaction_replace "$5" "$2" 0644
mac_launchd_bootstrap_job "gui/501" "$2" "gui/501/com.mac.tx" "com.mac.tx" "$7"
if [ "$6" = signal ]; then
  kill -TERM "$$"
fi
if [ "$6" = health-fail ]; then
  false
fi
if [ "$6" = fresh-health-fail ]; then
  false
fi
if [ "$6" = prequiesced-health-fail ]; then
  false
fi
mac_launchd_transaction_commit
"""
    return subprocess.run(
        [
            "/bin/bash",
            "-c",
            command,
            "bash",
            str(LAUNCHD_LIFECYCLE),
            str(plist),
            str(wrapper),
            str(staged_wrapper),
            str(staged_plist),
            mode,
            execution_mode,
            str(caller_cleanup),
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "FAKE_TX_STATE": str(state),
            "FAKE_TX_CALLS": str(calls),
            "FAKE_TX_MODE": mode,
            "FAKE_SUDO_CONTROL_CALLS": str(sudo_control_calls),
            "FAKE_SUDO_ARTIFACT_CALLS": str(sudo_artifact_calls),
            "MAC_LAUNCHD_POLL_INTERVAL_SECONDS": "0.01",
            "MAC_LAUNCHD_TX_RECOVERY_POLICY": recovery_policy,
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize(
    ("mode", "expected_rc", "expected_generation"),
    (
        ("success", 0, "new"),
        ("bootstrap-fail", 1, "old"),
        ("health-fail", 1, "old"),
        ("signal", 143, "old"),
    ),
)
def test_launchd_transaction_compensates_before_commit(
    tmp_path: Path,
    mode: str,
    expected_rc: int,
    expected_generation: str,
) -> None:
    result = _run_launchd_transaction(tmp_path, mode)
    case_dir = tmp_path / mode

    assert result.returncode == expected_rc, result.stderr
    assert (case_dir / "service.plist").read_text(encoding="utf-8") == (
        f"{expected_generation} plist\n"
    )
    assert (case_dir / "service-run").read_text(encoding="utf-8") == (
        f"{expected_generation} wrapper\n"
    )
    assert (case_dir / "state").read_text(encoding="utf-8") == "active\n"
    if expected_generation == "old":
        assert "restored prior launchd generation" in result.stderr


def test_launchd_transaction_retains_failed_generation_for_forward_repair(
    tmp_path: Path,
) -> None:
    result = _run_launchd_transaction(
        tmp_path, "health-fail", recovery_policy="retain-forward"
    )
    case_dir = tmp_path / "health-fail"

    assert result.returncode == 1, result.stderr
    assert (case_dir / "service.plist").read_text(encoding="utf-8") == "new plist\n"
    assert (case_dir / "service-run").read_text(encoding="utf-8") == "new wrapper\n"
    assert (case_dir / "state").read_text(encoding="utf-8") == "active\n"
    assert "retaining failed launchd generation for forward repair" in result.stderr
    assert "restored prior launchd generation" not in result.stderr


@pytest.mark.parametrize(
    ("mode", "expected_parent_rc"),
    (("success", 0), ("health-fail", 1), ("signal", 143)),
)
def test_launchd_transaction_chains_caller_exit_cleanup_exactly_once(
    tmp_path: Path, mode: str, expected_parent_rc: int
) -> None:
    result = _run_launchd_transaction(tmp_path, mode)
    cleanup = tmp_path / mode / "caller-cleanup"

    assert result.returncode in {0, 1, 143}, result.stderr
    assert cleanup.read_text(encoding="utf-8").splitlines() == [
        f"cleanup:{expected_parent_rc}"
    ]


def test_failed_fresh_install_removes_new_generation(tmp_path: Path) -> None:
    result = _run_launchd_transaction(tmp_path, "fresh-health-fail")
    case_dir = tmp_path / "fresh-health-fail"

    assert result.returncode == 1, result.stderr
    assert not (case_dir / "service.plist").exists()
    assert not (case_dir / "service-run").exists()
    assert (case_dir / "state").read_text(encoding="utf-8") == "inactive\n"
    assert "restored prior launchd generation" in result.stderr


def test_prequiesced_active_prestate_restarts_old_generation(
    tmp_path: Path,
) -> None:
    result = _run_launchd_transaction(tmp_path, "prequiesced-health-fail")
    case_dir = tmp_path / "prequiesced-health-fail"

    assert result.returncode == 1, result.stderr
    assert (case_dir / "service.plist").read_text(encoding="utf-8") == "old plist\n"
    assert (case_dir / "service-run").read_text(encoding="utf-8") == "old wrapper\n"
    assert (case_dir / "state").read_text(encoding="utf-8") == "active\n"
    assert "restored prior launchd generation" in result.stderr
    calls = (case_dir / "calls").read_text(encoding="utf-8").splitlines()
    assert calls.count("bootout gui/501/com.mac.tx") == 1
    first_bootstrap = next(
        index for index, call in enumerate(calls) if call.startswith("bootstrap ")
    )
    rollback_bootout = calls.index("bootout gui/501/com.mac.tx")
    assert first_bootstrap < rollback_bootout


@pytest.mark.parametrize(
    ("expected_state", "expected_error"),
    (
        (
            "active",
            "cannot expect active prior state without a canonical plist snapshot",
        ),
        ("unknown", "invalid expected prior launchd state"),
    ),
)
def test_expected_prior_state_override_rejects_unproved_values(
    tmp_path: Path,
    expected_state: str,
    expected_error: str,
) -> None:
    case_dir = tmp_path / expected_state
    fake_bin = case_dir / "bin"
    fake_bin.mkdir(parents=True)
    state = case_dir / "state"
    state.write_text("inactive\n", encoding="utf-8")
    plist = case_dir / "service.plist"
    launchctl = fake_bin / "launchctl"
    launchctl.write_text(
        """#!/bin/sh
set -eu
if [ "$1" = print ]; then
  echo 'Could not find service synthetic' >&2
  exit 113
fi
exit 64
""",
        encoding="utf-8",
    )
    launchctl.chmod(0o755)

    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            'set -euo pipefail; . "$1"; '
            'mac_launchd_transaction_begin "gui/501" "$2" '
            '"gui/501/com.mac.tx" "com.mac.tx"; '
            'mac_launchd_transaction_set_expected_prior_state "$3"',
            "bash",
            str(LAUNCHD_LIFECYCLE),
            str(plist),
            expected_state,
        ],
        env={
            **os.environ,
            "PATH": f"{fake_bin}:{os.environ['PATH']}",
            "MAC_LAUNCHD_COMMAND_TIMEOUT_SECONDS": "1",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not plist.exists()
    assert state.read_text(encoding="utf-8") == "inactive\n"


def test_system_transaction_rolls_back_control_and_root_artifacts_via_sudo(
    tmp_path: Path,
) -> None:
    result = _run_launchd_transaction(tmp_path, "health-fail", "system")
    case_dir = tmp_path / "health-fail"

    assert result.returncode == 1, result.stderr
    assert (case_dir / "service.plist").read_text(encoding="utf-8") == "old plist\n"
    assert (case_dir / "service-run").read_text(encoding="utf-8") == "old wrapper\n"
    assert stat.S_IMODE((case_dir / "service.plist").stat().st_mode) == 0o640
    assert stat.S_IMODE((case_dir / "service-run").stat().st_mode) == 0o750
    assert (case_dir / "state").read_text(encoding="utf-8") == "active\n"

    control_calls = (case_dir / "sudo-control-calls").read_text(
        encoding="utf-8"
    ).splitlines()
    assert control_calls == [
        "-n launchctl print gui/501/com.mac.tx",
        "-n launchctl print gui/501/com.mac.tx",
        "-n launchctl bootout gui/501/com.mac.tx",
        "-n launchctl print gui/501/com.mac.tx",
        "-n launchctl print gui/501/com.mac.tx",
        "-n launchctl enable gui/501/com.mac.tx",
        f"-n launchctl bootstrap gui/501 {case_dir / 'service.plist'}",
        "-n launchctl print gui/501/com.mac.tx",
        "-n launchctl print gui/501/com.mac.tx",
        "-n launchctl bootout gui/501/com.mac.tx",
        "-n launchctl print gui/501/com.mac.tx",
        "-n launchctl print gui/501/com.mac.tx",
        "-n launchctl enable gui/501/com.mac.tx",
        f"-n launchctl bootstrap gui/501 {case_dir / 'service.plist'}",
        "-n launchctl print gui/501/com.mac.tx",
    ]
    artifact_calls = (case_dir / "sudo-artifact-calls").read_text(
        encoding="utf-8"
    ).splitlines()
    assert len(artifact_calls) >= 9
    assert all(call.startswith("-n|") and call.endswith("|-c") for call in artifact_calls)


def _remove_owned_absent(
    path: Path, mode: str = "user", *, geteuid: int | None = None
) -> subprocess.CompletedProcess[str]:
    """Run mac_launchd_remove_owned_absent_file against ``path``.

    When ``geteuid`` is provided the lifecycle's Python bounded runner is
    shimmed to report that effective uid, which lets the ownership contract be
    exercised without a second real identity.
    """

    env = os.environ.copy()
    if geteuid is None:
        script = 'set -euo pipefail; . "$1"; mac_launchd_remove_owned_absent_file "$2" "$3"'
    else:
        # Shim the lifecycle's bounded Python runner so os.geteuid() reports a
        # chosen effective uid, exercising the ownership contract without a
        # second real identity.  The forced-euid prelude is prepended to the
        # exact program bytes the function would otherwise have executed.
        env["MAC_TEST_FORCE_EUID"] = str(geteuid)
        prelude = (
            "import os as _os; "
            "_os.geteuid = lambda: int(_os.environ['MAC_TEST_FORCE_EUID']); "
        )
        script = (
            'set -euo pipefail; . "$1"\n'
            "mac_launchd_run_python_bounded() {\n"
            '  local mode="$1" timeout="$2" program="$3"; shift 3\n'
            '  : "$mode" "$timeout"\n'
            f'  command python3 -c "{prelude}$program" "$@"\n'
            "}\n"
            'mac_launchd_remove_owned_absent_file "$2" "$3"'
        )
    return subprocess.run(
        ["/bin/bash", "-c", script, "bash", str(LAUNCHD_LIFECYCLE), str(path), mode],
        capture_output=True,
        text=True,
        env=env,
    )


def test_remove_owned_absent_is_idempotent_for_an_already_absent_path(
    tmp_path: Path,
) -> None:
    result = _remove_owned_absent(tmp_path / "mac-fleet.conf")
    assert result.returncode == 0, result.stderr


def test_remove_owned_absent_removes_an_empty_transaction_owned_file(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "mac-fleet.conf"
    artifact.write_text("", encoding="utf-8")
    result = _remove_owned_absent(artifact)
    assert result.returncode == 0, result.stderr
    assert not artifact.exists()


def test_remove_owned_absent_removes_a_present_transaction_owned_file(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "mac-fleet.conf"
    artifact.write_text("[program:mac-worker]\n", encoding="utf-8")
    result = _remove_owned_absent(artifact)
    assert result.returncode == 0, result.stderr
    assert not artifact.exists()


def test_remove_owned_absent_refuses_a_symlink_and_keeps_its_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "successor.conf"
    target.write_text("successor definition\n", encoding="utf-8")
    link = tmp_path / "mac-fleet.conf"
    link.symlink_to(target)
    result = _remove_owned_absent(link)
    assert result.returncode != 0
    assert "symlink" in result.stderr
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "successor definition\n"


def test_remove_owned_absent_refuses_a_directory(tmp_path: Path) -> None:
    directory = tmp_path / "mac-fleet.conf"
    directory.mkdir()
    result = _remove_owned_absent(directory)
    assert result.returncode != 0
    assert "directory" in result.stderr
    assert directory.is_dir()


def test_remove_owned_absent_refuses_a_non_regular_artifact(tmp_path: Path) -> None:
    fifo = tmp_path / "mac-fleet.conf"
    os.mkfifo(fifo)
    result = _remove_owned_absent(fifo)
    assert result.returncode != 0
    assert "non-regular" in result.stderr
    assert fifo.exists()


def test_remove_owned_absent_refuses_a_foreign_owned_conflicting_successor(
    tmp_path: Path,
) -> None:
    successor = tmp_path / "mac-fleet.conf"
    successor.write_text("foreign successor\n", encoding="utf-8")
    foreign_uid = os.geteuid() + 4096
    result = _remove_owned_absent(successor, geteuid=foreign_uid)
    assert result.returncode != 0
    assert "foreign-owned" in result.stderr
    assert successor.read_text(encoding="utf-8") == "foreign successor\n"


def test_remove_owned_absent_removes_a_matching_owner_under_shimmed_euid(
    tmp_path: Path,
) -> None:
    artifact = tmp_path / "mac-fleet.conf"
    artifact.write_text("owned\n", encoding="utf-8")
    result = _remove_owned_absent(artifact, geteuid=os.geteuid())
    assert result.returncode == 0, result.stderr
    assert not artifact.exists()
