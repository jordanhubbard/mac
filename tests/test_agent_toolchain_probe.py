"""The worker advertises c/make/python3 only when its sandbox can run them.

mac-agent-service probed each tool with `openshell run --quiet -- command -v`,
a subcommand no OpenShell release has. Every probe failed, so from at least
2026-10-01 no worker advertised c, make or python3, and work needing them could
only match a worker whose capability list had been set by hand.

The probe now creates one throwaway sandbox the way a task does (same policy,
same --from image) and RUNS each tool, because a tool on PATH can still be
unusable: bookworm's make 4.3 could not start a single recipe under OpenShell
0.1's seccomp filter.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SERVICE = ROOT / "deploy" / "bin" / "mac-agent-service"

FAKE_OPENSHELL = """#!/usr/bin/env bash
# Records every call; `sandbox create ... -- CMD` runs CMD locally, as the
# sandbox would, with PATH shims from FAKE_SHIMS taking precedence.
printf '%s\\n' "$*" >> "$FAKE_LOG"
if [ "$1 $2" = "sandbox create" ]; then
  while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
  shift
  PATH="${FAKE_SHIMS:+$FAKE_SHIMS:}$PATH" exec "$@"
fi
exit 0
"""


def _probe_function() -> str:
    source = SERVICE.read_text(encoding="utf-8")
    match = re.search(r"^probe_sandbox_toolchain\(\) \{\n.*?^\}\n", source, re.M | re.S)
    assert match, "probe_sandbox_toolchain() is missing from mac-agent-service"
    return match.group(0)


def _run_probe(tmp_path: Path, *, shims: dict[str, str] | None = None, policy: bool = True):
    fake = tmp_path / "openshell"
    fake.write_text(FAKE_OPENSHELL, encoding="utf-8")
    fake.chmod(0o755)
    shim_dir = tmp_path / "shims"
    shim_dir.mkdir()
    for name, body in (shims or {}).items():
        shim = shim_dir / name
        shim.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
        shim.chmod(0o755)
    policy_file = tmp_path / "policy.yaml"
    if policy:
        policy_file.write_text("version: 1\n", encoding="utf-8")
    log = tmp_path / "calls.log"
    script = "set -euo pipefail\n" + _probe_function() + 'probe_sandbox_toolchain | tr "\\n" " "\n'
    result = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=120,
        env={
            "PATH": "/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin",
            "HOME": str(tmp_path),
            "MAC_OPENSHELL_BIN": str(fake),
            "MAC_OPENSHELL_POLICY": str(policy_file),
            "MAC_OPENSHELL_CREATE_ARGS": "--from ghcr.io/example/runtime@sha256:" + "0" * 64,
            "FAKE_LOG": str(log),
            "FAKE_SHIMS": str(shim_dir),
        },
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return result, calls


needs_toolchain = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("cc", "make", "python3")),
    reason="the probe runs cc, make and python3 for real",
)


def test_the_probe_no_longer_uses_a_subcommand_openshell_does_not_have():
    assert " run --quiet" not in SERVICE.read_text(encoding="utf-8")


@needs_toolchain
def test_a_sandbox_that_runs_every_tool_proves_all_three(tmp_path):
    result, calls = _run_probe(tmp_path)

    assert result.stdout.split() == ["c", "make", "python3"], result.stderr
    create = next(call for call in calls if call.startswith("sandbox create"))
    assert "--policy %s" % (tmp_path / "policy.yaml") in create
    assert "--from ghcr.io/example/runtime@sha256:" in create
    name = re.search(r"--name (\S+)", create).group(1)
    assert len(name) <= 19
    assert "sandbox delete %s" % name in calls


@needs_toolchain
def test_a_make_that_cannot_run_a_recipe_is_not_advertised(tmp_path):
    """The OpenShell 0.1 failure: make is on PATH and still useless."""
    result, _calls = _run_probe(
        tmp_path,
        shims={"make": "echo 'make: /bin/sh: Operation not permitted' >&2; exit 2"},
    )

    assert result.stdout.split() == ["c", "python3"], result.stderr


def test_without_a_policy_nothing_is_proved_and_no_sandbox_is_created(tmp_path):
    result, calls = _run_probe(tmp_path, policy=False)

    assert result.stdout.split() == []
    assert not any(call.startswith("sandbox create") for call in calls)


def test_a_failed_probe_cannot_stop_the_service():
    source = SERVICE.read_text(encoding="utf-8")
    assert "{ probe_sandbox_toolchain || true; }" in source
