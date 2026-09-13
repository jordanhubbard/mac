"""A requested verifier profile must exist before repository code can run."""

from __future__ import annotations

import json
import shlex
import subprocess
import types

import pytest

from mac import gitops, services

HEAD = "a" * 40
READY = "[hub-verifier-profile] bounded-tmpfs ready"
UNAVAILABLE = "hub verifier resource profile unavailable"


def _invoke(monkeypatch, *, output="passed", returncode=0):
    calls = []

    def run(argv, **kwargs):
        calls.append(list(argv))
        if argv[0] == "git" and "rev-parse" in argv:
            return subprocess.CompletedProcess(argv, 0, HEAD + "\n", "")
        if argv[0] in {"git", "tar"} or "delete" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, returncode, output, "")

    monkeypatch.setattr(services.subprocess, "run", run)
    monkeypatch.setattr(gitops, "askpass_remote_auth", lambda url: (url, {}))
    monkeypatch.setenv(
        "MAC_HUB_VERIFY_IMAGE",
        "ghcr.io/jordanhubbard/mac-openshell-runtime@sha256:" + "a" * 64,
    )
    monkeypatch.delenv("MAC_OPENSHELL_GC", raising=False)
    result = services.ControlPlane._hub_verify_run_contract_test(
        types.SimpleNamespace(),
        "https://example.invalid/repo.git",
        "branch",
        HEAD,
        "run-repository-tests",
        "bootstrap-repository",
    )
    return result, calls


def _create(calls):
    return next(argv for argv in calls if "create" in argv)


@pytest.mark.parametrize("profile", [None, "", "default"])
def test_default_verifier_profile_preserves_driver_behavior(monkeypatch, profile):
    if profile is None:
        monkeypatch.delenv("MAC_HUB_VERIFY_PROFILE", raising=False)
    else:
        monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", profile)
    (rc, _), calls = _invoke(monkeypatch)
    assert rc == 0
    argv = _create(calls)
    assert not {"--cpu", "--memory", "--driver-config-json"}.intersection(argv)
    assert not any(value.startswith(("TMPDIR=", "MAC_TEST_JOBS=")) for value in argv)
    assert READY not in argv[-1]


def test_bounded_profile_requests_native_limits_and_local_test_storage(monkeypatch):
    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    (rc, _), calls = _invoke(monkeypatch, output=READY + "\npassed")
    assert rc == 0
    argv = _create(calls)
    assert argv[argv.index("--cpu") + 1] == "12"
    assert argv[argv.index("--memory") + 1] == "32Gi"
    driver = json.loads(argv[argv.index("--driver-config-json") + 1])
    assert driver == {
        "docker": {
            "mounts": [
                {
                    "type": "tmpfs",
                    "target": "/sandbox/test-storage",
                    "size_bytes": 8 * 1024**3,
                    "mode": 0o1777,
                    "options": ["exec"],
                }
            ]
        }
    }
    env = [argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--env"]
    assert "TMPDIR=/sandbox/test-storage" in env
    assert "MAC_TEST_JOBS=8" in env
    assert "MAC_TEST_PG_LOCAL=1" in env
    assert not any(value.startswith("MAC_TEST_PG_URL=") for value in env)
    command = argv[-1]
    assert command.index(READY) < command.index("bootstrap-repository")
    assert command.index("bootstrap-repository") < command.index("run-repository-tests")


@pytest.mark.parametrize("profile", ["ram", "bounded-tmpfs; echo injected"])
def test_unknown_profile_stops_before_cloning_or_creating(monkeypatch, profile):
    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", profile)
    (rc, output), calls = _invoke(monkeypatch)
    assert rc != 0
    assert UNAVAILABLE in output
    assert services.hub_verification_unavailable_reason(output) == UNAVAILABLE
    assert calls == []


@pytest.mark.parametrize("returncode", [0, 1])
def test_backend_without_profile_proof_cannot_produce_a_pass(monkeypatch, returncode):
    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    (rc, output), calls = _invoke(
        monkeypatch, output="backend does not support requested mount", returncode=returncode
    )
    assert rc != 0
    assert "backend does not support requested mount" in output
    assert services.hub_verification_unavailable_reason(output) == UNAVAILABLE
    assert calls[-1][1:3] == ["sandbox", "delete"]


@pytest.mark.parametrize(
    "system,filesystem,expected",
    [("Linux", "tmpfs", 0), ("Linux", "overlayfs", 96), ("Darwin", "tmpfs", 96)],
)
def test_storage_preflight_gates_repository_execution(
    monkeypatch, tmp_path, system, filesystem, expected
):
    real_run = subprocess.run
    for name, value in [("uname", system), ("stat", filesystem)]:
        path = tmp_path / name
        path.write_text("#!/bin/sh\necho " + value + "\n")
        path.chmod(0o755)
    monkeypatch.setattr(services, "SANDBOX_BASE_PATH", str(tmp_path) + ":/usr/bin:/bin")
    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    _, calls = _invoke(monkeypatch, output=READY)
    command = _create(calls)[-1]
    assert READY in command
    preflight = command.split("cd /sandbox &&", 1)[0]
    # Replace only the mount operand, not the same prefix inside the fake
    # command directory on Linux sandboxes (whose pytest TMPDIR is this mount).
    preflight = preflight.replace(" /sandbox/test-storage ", " " + shlex.quote(str(tmp_path)) + " ")
    marker = tmp_path / "repository-started"
    result = real_run(
        ["/bin/bash", "-c", preflight + 'touch "$1"', "preflight", str(marker)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected
    assert marker.exists() is (expected == 0)
    assert (READY in result.stdout) is (expected == 0)
    if expected:
        assert UNAVAILABLE in result.stderr
