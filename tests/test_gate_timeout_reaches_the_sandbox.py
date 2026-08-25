"""A timeout the operator sets must reach the code that enforces it.

The repository gate's deadline is read INSIDE the sandbox:

    timeout = float(os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "1800"))

but the environment handed to the sandbox carried only HOME, MAC_TASK_FILE and
MAC_TASK_WORKSPACE. So the in-script default of 1800s applied regardless of the
host's configuration.

Observed on the fleet: a worker with MAC_WORKER_REPOSITORY_TEST_TIMEOUT=5400
failed three consecutive attempts with

    repository verifier exited with status 124:
    repository test command timed out after 1800.0s

while the operator read 5400 in mac.env and concluded the setting had been
applied. The host waited the longer time; the script inside kept killing the
run at thirty minutes.

A knob that silently does nothing is worse than no knob: it ends the
investigation at the wrong place.
"""

from __future__ import annotations

import pytest

from mac import executor_sandbox


@pytest.fixture()
def workspace(tmp_path):
    path = tmp_path / "task_probe"
    path.mkdir()
    return path


def _verification_env(monkeypatch, workspace, steps=None, **env):
    """Return the environment the sandbox verification script is given.

    Pass `steps` to also record the openshell argv the verification issues.
    """
    captured = {}

    if steps is not None:
        monkeypatch.setattr(
            executor_sandbox,
            "_sandbox_step",
            lambda argv, **kwargs: (steps.append(list(argv)), (True, ""))[1],
        )

    def capture(environment=None):
        captured.update(environment or {})
        return "# script"

    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(executor_sandbox, "_sandbox_repository_verification_shell", capture)
    monkeypatch.setattr(
        executor_sandbox, "_repository_contract_test_command", lambda task: "run.sh"
    )
    monkeypatch.setattr(executor_sandbox, "task_is_repo_coupled", lambda task: True)
    monkeypatch.setattr(
        executor_sandbox,
        "metadata_declares_read_only_report_repository",
        lambda metadata: False,
    )
    monkeypatch.setattr(
        executor_sandbox,
        "_sandbox_repository_environment",
        lambda ws, sub: {"MAC_REPO_TEST_COMMAND": "run.sh"},
    )
    monkeypatch.setattr(
        executor_sandbox,
        "_sandbox_run_repository_verification_exec",
        lambda *a, **k: executor_sandbox._SandboxRepositoryVerificationResult(True),
    )
    executor_sandbox._sandbox_run_repository_verification(
        "sandbox-probe", workspace.name, workspace, {"id": "task_probe"}
    )
    return captured


def test_the_configured_timeout_reaches_the_sandbox(monkeypatch, workspace):
    """The live failure: the host said 5400, the sandbox enforced 1800."""
    env = _verification_env(monkeypatch, workspace, MAC_WORKER_REPOSITORY_TEST_TIMEOUT="5400")

    assert env.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT") == "5400"


def test_the_bootstrap_timeout_reaches_it_too(monkeypatch, workspace):
    """Dependency setup has its own deadline and the same delivery problem."""
    env = _verification_env(monkeypatch, workspace, MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT="2400")

    assert env.get("MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT") == "2400"


def test_an_unset_timeout_is_not_invented(monkeypatch, workspace):
    """Forwarding an empty value would override the in-script default with
    nonsense; absence must stay absent."""
    monkeypatch.delenv("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", raising=False)
    monkeypatch.delenv("MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT", raising=False)

    env = _verification_env(monkeypatch, workspace)

    assert "MAC_WORKER_REPOSITORY_TEST_TIMEOUT" not in env
    assert "MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT" not in env


def test_forwarding_does_not_clobber_the_sandbox_name(monkeypatch, workspace):
    """The first cut of this feature wrote `for name in (...)`, which shadowed
    the `name` parameter holding the sandbox to talk to.

    Every subsequent openshell call then addressed a sandbox called
    "MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT", so no repository verification
    could have run anywhere on the fleet. The tests above still passed, because
    the environment is captured before the upload -- so the timeout arrived
    correctly at a sandbox that did not exist.
    """
    steps = []

    _verification_env(
        monkeypatch,
        workspace,
        steps=steps,
        MAC_WORKER_REPOSITORY_TEST_TIMEOUT="5400",
        MAC_WORKER_REPOSITORY_BOOTSTRAP_TIMEOUT="2400",
    )

    uploads = [step for step in steps if step and step[0] == "upload"]
    assert uploads, "verification issued no upload"
    for step in uploads:
        assert step[1] == "sandbox-probe", (
            "the upload addressed %r instead of the sandbox it was given" % step[1]
        )
