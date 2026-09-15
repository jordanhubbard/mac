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
        if argv[0] in {"git", "tar"} or "delete" in argv or "create" in argv or "upload" in argv:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "exec" in argv and "bootstrap-repository" in argv[-1]:
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


def _exec(calls, *, initialize=False):
    marker = "tar xzf repo.tgz"
    return next(
        argv
        for argv in calls
        if "exec" in argv and ((marker in argv[-1]) is initialize)
    )


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
    assert READY not in _exec(calls, initialize=True)[-1]


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
    bootstrap = _exec(calls, initialize=True)[-1]
    test = _exec(calls, initialize=False)[-1]
    assert bootstrap.index(READY) < bootstrap.index("bootstrap-repository")
    assert calls.index(_exec(calls, initialize=True)) < calls.index(_exec(calls, initialize=False))
    assert "run-repository-tests" in test


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
    command = _exec(calls, initialize=True)[-1]
    assert READY in command
    preflight = "export PATH=" + command.split("export PATH=", 1)[1].split(
        "cd /sandbox/repo", 1
    )[0]
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


@pytest.mark.parametrize("profile", [None, "default", "bounded-tmpfs"])
def test_worker_create_applies_profile_before_toolchain(monkeypatch, tmp_path, profile):
    from mac import executor_sandbox as sandbox

    if profile is None:
        monkeypatch.delenv("MAC_HUB_VERIFY_PROFILE", raising=False)
    else:
        monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", profile)
    monkeypatch.setattr(sandbox, "_resolve_openshell_policy", lambda: "/policy.yaml")
    monkeypatch.setattr(sandbox, "_sandbox_credential_upload_argv", lambda: [])
    argv = sandbox._build_sandbox_create_argv(
        "profile-test",
        tmp_path,
        "workspace",
        ["python", "-m", "mac.agent_command"],
        extra_create_argv=["--from", "approved-image"],
    )
    if profile == "bounded-tmpfs":
        assert argv[argv.index("--cpu") + 1] == "12"
        assert argv[argv.index("--memory") + 1] == "32Gi"
        assert (
            json.loads(argv[argv.index("--driver-config-json") + 1])["docker"]["mounts"][0]["type"]
            == "tmpfs"
        )
        command = argv[-1]
        assert command.index(". ./.mac-openshell-env.sh") < command.index(READY)
        assert command.index(READY) < command.index(". ./.mac-sandbox-toolchain.sh")
    else:
        assert not {"--cpu", "--memory", "--driver-config-json"}.intersection(argv)
        assert READY not in argv[-1]


@pytest.mark.parametrize("lane", ["worker-exec", "read-only-verifier"])
@pytest.mark.parametrize("filesystem,expected", [("tmpfs", 0), ("overlayfs", 96)])
def test_fresh_worker_shell_reasserts_profile_before_setup(
    monkeypatch, tmp_path, lane, filesystem, expected
):
    import os
    from mac import executor_sandbox as sandbox

    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    for name, value in [("uname", "Linux"), ("stat", filesystem)]:
        path = tmp_path / name
        path.write_text("#!/bin/sh\necho " + value + "\n")
        path.chmod(0o755)
    marker = tmp_path / "toolchain-environment.json"
    # Execute the real generated shell up to its toolchain boundary, starting
    # with unrelated agent-shell settings. The setup probe exits before any
    # repository command; failed preflight must never reach it.
    monkeypatch.setattr(
        sandbox,
        "_sandbox_toolchain_setup_shell",
        lambda: (
            "mac_sandbox_toolchain_setup() { "
            + "printf '%s\\n' \"$TMPDIR $MAC_TEST_JOBS\" > "
            + shlex.quote(str(marker))
            + "; exit 0; }"
        ),
    )
    environment = {
        "MAC_TASK_WORKSPACE": str(tmp_path),
        "TMPDIR": "/agent-stale",
        "MAC_TEST_JOBS": "99",
    }
    shell = (
        sandbox._sandbox_repository_verification_shell(environment)
        if lane == "worker-exec"
        else sandbox._sandbox_read_only_repository_verification_shell(environment)
    )
    shell = shell.replace(" /sandbox/test-storage ", " " + shlex.quote(str(tmp_path)) + " ")
    result = subprocess.run(
        ["/bin/bash", "--noprofile", "--norc", "-c", shell],
        env={"PATH": str(tmp_path) + os.pathsep + "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == expected, result.stderr
    assert marker.exists() is (expected == 0)
    if not expected:
        assert marker.read_text().strip() == "/sandbox/test-storage 8"
    else:
        assert UNAVAILABLE in result.stderr


@pytest.mark.parametrize("override", ["--cpu 24", "--memory=64Gi", "--driver-config-json {}"])
def test_worker_profile_rejects_ambiguous_resource_overrides(monkeypatch, override):
    from mac.openshell_runtime import verifier_profile_create_args

    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    with pytest.raises(ValueError, match="conflicts"):
        verifier_profile_create_args(shlex.split(override))


def test_report_profile_does_not_authorize_operator_mounts(monkeypatch):
    from mac import executor_sandbox as sandbox

    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    monkeypatch.setenv(
        "MAC_OPENSHELL_CREATE_ARGS", '--driver-config-json \'{"docker":{"mounts":[]}}\''
    )
    monkeypatch.delenv("MAC_OPENSHELL_SANDBOX_NAME", raising=False)
    monkeypatch.setattr(sandbox, "_managed_openshell_runtime_image_ref", lambda: "approved-image")
    with pytest.raises(ValueError, match="forbidden"):
        sandbox._read_only_report_extra_create_argv(require_approval=False)


def test_separate_read_only_verifier_requests_controller_profile(monkeypatch, tmp_path):
    from mac import executor_sandbox as sandbox
    from tests.test_openshell_sandbox import _exact_read_only_report_workspace

    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    workspace, _, task = _exact_read_only_report_workspace(tmp_path)
    monkeypatch.setattr(
        sandbox, "_read_only_verifier_extra_create_argv", lambda: ["--from", "approved-image"]
    )
    calls = []

    def step(args, *, timeout):
        calls.append(args)
        if args[0] == "create":
            assert args[args.index("--cpu") + 1] == "12"
            assert args[args.index("--memory") + 1] == "32Gi"
            mount = json.loads(args[args.index("--driver-config-json") + 1])["docker"]["mounts"][0]
            assert mount["target"] == "/sandbox/test-storage"
            assert mount["size_bytes"] == 8 * 1024**3
            upload = __import__("pathlib").Path(args[args.index("--upload") + 1].split(":", 1)[0])
            script = (upload / ".mac-sandbox-repository-verify.sh").read_text()
            assert script.index(READY) < script.index("mac_sandbox_toolchain_setup")
            assert "mac.read_only_report_verifier" in script
            assert timeout > 0
            return False, "backend could not create tmpfs"
        if args[0] == "download":
            return False, "no result"
        assert args[0] == "delete"
        return True, ""

    monkeypatch.setattr(sandbox, "_sandbox_step", step)
    assert not sandbox._sandbox_run_read_only_repository_verification(
        "agent-sandbox", workspace, task
    )
    assert [args[0] for args in calls] == ["create", "download", "delete"]
    assert not (workspace / sandbox._TRUSTED_READ_ONLY_VERIFICATION_FILE).exists()


def test_coding_route_probe_does_not_require_repository_test_storage(monkeypatch, tmp_path):
    from mac import executor_sandbox as sandbox

    monkeypatch.setenv("MAC_HUB_VERIFY_PROFILE", "bounded-tmpfs")
    monkeypatch.setattr(sandbox, "_resolve_openshell_policy", lambda: "/policy.yaml")
    monkeypatch.setattr(sandbox, "_sandbox_credential_upload_argv", lambda: [])
    monkeypatch.setattr(
        sandbox, "_openshell_extra_create_argv", lambda: ["--from", "approved-image"]
    )
    argv = sandbox._build_sandbox_probe_argv(
        "coding-route-probe", ["python", "-m", "mac.agent_command"], tmp_path
    )
    assert "--driver-config-json" not in argv
    assert READY not in argv[-1]
