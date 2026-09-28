"""The staged-bundle byte count must be exactly one integer on Linux.

``stage_remote_file_once_exact`` hands the controller-side byte count to a
remote ``python3`` probe that parses it with ``int()``.  The historical
``stat -f %z "$f" 2>/dev/null || stat -c %s "$f"`` spelling is not portable in
the direction it looks portable: GNU stat reads ``-f`` as "filesystem status",
writes a multi-line filesystem report for the file to *stdout*, and only then
exits nonzero, so the fallback concatenates the real byte count onto that
report.  A Linux hub therefore failed stage-bundle before any source/service
replacement, with the remote integer parser correctly rejecting the combined
output.

These tests run the real deploy functions on Linux against a local stand-in for
the remote node, proving the staging probe accepts a correctly staged file and
still rejects one whose bytes changed.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "deploy-mac-fleet.sh"


def _function(source: str, name: str) -> str:
    """Return the shell text of ``name`` up to its column-zero closing brace."""

    marker = f"\n{name}() {{\n"
    start = source.index(marker) + 1
    end = source.index("\n}\n", start) + len("\n}\n")
    return source[start:end]


def _harness(body: str) -> str:
    source = DEPLOY.read_text(encoding="utf-8")
    return "\n".join(
        [
            "set -uo pipefail",
            f"PYTHON_BIN={shlex.quote(sys.executable)}",
            _function(source, "sha256_file"),
            _function(source, "file_byte_size"),
            _function(source, "stage_remote_file_once_exact"),
            # Stand in for the node: the fenced command is assembled verbatim
            # and executed locally, so the real remote probe -- including its
            # size, ownership, mode, link-count, no-follow and digest checks --
            # is what answers.
            "remote_deployment_fenced_exec() {",
            '  local _deployment_id="$1" _fence="$2" part rendered=""',
            "  shift 2",
            '  for part in "$@"; do rendered+=" $(printf %q "$part")"; done',
            '  printf %s "${rendered# }"',
            "}",
            'ssh() { local remote_command="${!#}"; bash -c "$remote_command"; }',
            "ssh_target_args() { printf '%s\\0' -o BatchMode=yes node.example; }",
            "fenced_remote_upload() { printf 'uploaded\\n'; }",
            body,
        ]
    )


def _run(body: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _harness(body), "staging", *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )


STAGE = (
    "set +e\n"
    'stage_remote_file_once_exact node.example deploy-1 "$1" "$2"\n'
    'printf "rc=%s\\n" "$?"\n'
)


def test_byte_count_helper_emits_only_the_integer_size(tmp_path: Path) -> None:
    payload = tmp_path / "release.tar.gz"
    payload.write_bytes(b"immutable staged bytes\n")

    result = _run('file_byte_size "$1"', str(payload))

    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{payload.stat().st_size}\n"
    assert result.stdout.strip().isdigit()


def test_byte_count_helper_refuses_to_measure_through_a_symlink(tmp_path: Path) -> None:
    payload = tmp_path / "release.tar.gz"
    payload.write_bytes(b"immutable staged bytes\n")
    link = tmp_path / "release-link.tar.gz"
    link.symlink_to(payload)

    result = _run('file_byte_size "$1"', str(link))

    assert result.returncode != 0
    assert "not a regular file" in result.stderr


def test_exact_staging_accepts_the_correct_size_on_linux(tmp_path: Path) -> None:
    source = tmp_path / "release.tar.gz"
    source.write_bytes(b"immutable staged bytes\n")
    destination = tmp_path / "staged-release.tar.gz"
    destination.write_bytes(source.read_bytes())
    destination.chmod(0o600)

    result = _run(STAGE, str(source), str(destination))

    assert result.returncode == 0, result.stderr
    assert result.stdout == "rc=0\n"
    assert "invalid staged item state" not in result.stderr


def test_exact_staging_still_rejects_changed_bytes(tmp_path: Path) -> None:
    source = tmp_path / "release.tar.gz"
    source.write_bytes(b"immutable staged bytes\n")
    destination = tmp_path / "staged-release.tar.gz"
    # Same byte count, different content: only the digest check can catch this.
    destination.write_bytes(b"tampered staged bytes!\n")
    destination.chmod(0o600)
    assert destination.stat().st_size == source.stat().st_size

    result = _run(STAGE, str(source), str(destination))

    assert "rc=1" in result.stdout
    assert "existing staged item differs from controller digest" in result.stderr
    assert "invalid staged item state" in result.stderr


def test_exact_staging_still_rejects_a_wrong_sized_or_wrong_mode_item(
    tmp_path: Path,
) -> None:
    source = tmp_path / "release.tar.gz"
    source.write_bytes(b"immutable staged bytes\n")

    truncated = tmp_path / "staged-truncated.tar.gz"
    truncated.write_bytes(b"immutable staged byte\n")
    truncated.chmod(0o600)
    result = _run(STAGE, str(source), str(truncated))
    assert "rc=1" in result.stdout
    assert "existing staged item is unsafe" in result.stderr

    loose = tmp_path / "staged-loose.tar.gz"
    loose.write_bytes(source.read_bytes())
    loose.chmod(0o644)
    result = _run(STAGE, str(source), str(loose))
    assert "rc=1" in result.stdout
    assert "existing staged item is unsafe" in result.stderr


def test_exact_staging_uploads_a_missing_item(tmp_path: Path) -> None:
    source = tmp_path / "release.tar.gz"
    source.write_bytes(b"immutable staged bytes\n")
    destination = tmp_path / "absent.tar.gz"

    result = _run(STAGE, str(source), str(destination))

    assert result.returncode == 0, result.stderr
    assert "uploaded" in result.stdout
    assert "rc=0" in result.stdout


def test_deploy_script_no_longer_uses_the_contaminated_stat_fallback() -> None:
    source = DEPLOY.read_text(encoding="utf-8")
    staging = _function(source, "stage_remote_file_once_exact")
    assert "stat -f" not in staging
    assert "stat -c" not in staging
    assert 'expected_size="$(file_byte_size "$source")"' in staging
    # No other controller-side command may reintroduce the fallback either;
    # the surviving mentions are explanatory comments.
    executable = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    assert "stat -f %z" not in executable


@pytest.mark.skipif(sys.platform != "linux", reason="GNU stat is a Linux behaviour")
def test_gnu_stat_filesystem_flag_really_does_contaminate_stdout(tmp_path: Path) -> None:
    """Pin the platform behaviour the fix exists for."""

    payload = tmp_path / "release.tar.gz"
    payload.write_bytes(b"immutable staged bytes\n")
    probe = subprocess.run(
        ["bash", "-c", 'stat -f %z "$1" 2>/dev/null || stat -c %s "$1"', "probe", str(payload)],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "LC_ALL": "C"},
        timeout=60,
    )
    assert probe.returncode == 0
    assert probe.stdout.strip() != str(payload.stat().st_size)
    assert not probe.stdout.strip().isdigit()
