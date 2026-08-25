"""Container contracts for OpenShell confinement + NeMo Relay translation.

These are the Docker Engine/Moby container tests that exercise the integration
chain against the real built image:

  1. ``MAC_OPENSHELL_SANDBOX=1`` makes ``executor_sandbox._openshell_enabled()``
     true, and ``_build_sandbox_create_argv()`` produces an ``openshell sandbox
     create`` argv that ALWAYS carries an explicit ``--policy`` (the guardrail
     specification). Confinement that silently falls back to OpenShell's own
     image-default profile is the failure this exists to catch.
  2. ``MAC_OPENSHELL_SANDBOX=0`` leaves the executor unconfined by the per-task
     wrap (the supervisor model is a separate seam).
  3. An OpenShell OCSF event translates via
     ``relay_observability.ocsf_to_observation()`` into a mac observation, and a
     denied-egress decision is escalated to at least ``warning``.

The container topology (executor + OpenTelemetry collector) lives in
``docker-compose.e2e.yaml`` / ``../../Dockerfile.e2e`` / ``otelcol-config.yaml``.

Run them::

    TEST_CONTAINER_CONTRACT=1 pytest tests/e2e/test_openshell_nemo_relay.py -v -m container_contract

They are marked ``container_contract`` and skipped unless
``TEST_CONTAINER_CONTRACT=1`` (or the legacy ``TEST_DOCKER_E2E=1``) is set and
``docker compose`` is available. These tests call MAC internals inside the
container, so they are intentionally not described as black-box E2E tests.

HISTORY -- why this file is written defensively.
This module and its scheduled CI job (``container-contract`` in
.github/workflows/ci.yml) were permanently green while running nothing, for
three independent reasons, none of which produced a red build:

  * The guard below looked for ``tests/e2e/Dockerfile.e2e``. That file has never
    existed; ``Dockerfile.e2e`` was added at the REPO ROOT by the same commit
    (417e7ab0). All three real tests skipped on every scheduled run and the job
    reported success having executed one assertion -- that the compose file the
    module itself points at exists.
  * ``docker-compose.e2e.yaml`` set ``context: .``, i.e. ``tests/e2e``, so even
    with the guard fixed the image could not have been built.
  * ``Dockerfile.e2e`` itself could not build: ``COPY tests/`` is excluded by
    the ``.dockerignore`` allowlist, and ``openshell==0.0.59`` is both withdrawn
    from PyPI and ``Requires-Python >=3.12`` on a 3.11 base.

When the paths were finally fixed, the tests failed: they drove
``task_executor._maybe_wrap_openshell()``, a function that no longer exists
anywhere in ``src/`` (``task_executor`` is now an alias shim for
``executor_sandbox``, and confinement goes through ``openshell sandbox
create``). They have therefore been retargeted at the seam that exists.

If you make any of these skip again, make it LOUD: name the reason.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

CONTAINER_CONTRACT = (
    os.environ.get("TEST_CONTAINER_CONTRACT", "").strip() == "1"
    or os.environ.get("TEST_DOCKER_E2E", "").strip() == "1"
)
COMPOSE_FILE = Path(__file__).parent / "docker-compose.e2e.yaml"

REPO_ROOT = Path(__file__).resolve().parents[2]
#: The executor image definition. At the REPO ROOT, not next to this file --
#: pointing it at ``Path(__file__).parent`` is what kept the whole suite skipped.
DOCKERFILE_E2E = REPO_ROOT / "Dockerfile.e2e"


def _docker_available() -> bool:
    """Return True if Docker Engine/Moby compose is functional."""
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _skip_reason() -> str:
    """A SPECIFIC reason, never a silent skip.

    A missing Dockerfile is a repository defect, not an environment limitation,
    so it is reported as such rather than folded into "docker unavailable".
    """
    if not DOCKERFILE_E2E.exists():
        return (
            "REPOSITORY DEFECT: %s does not exist, so the executor image cannot "
            "be built. This is not an environment limitation -- fix the tree." % DOCKERFILE_E2E
        )
    if not _docker_available():
        return (
            "`docker compose version` failed: no working Docker Engine/Moby on "
            "this host. The container contracts need a real daemon; they are not "
            "reproducible without one."
        )
    return ""


_SKIP_REASON = _skip_reason()


def _run_in_executor(env: dict, script: str) -> subprocess.CompletedProcess:
    argv = ["docker", "compose", "-f", str(COMPOSE_FILE), "run", "--rm"]
    for key, value in env.items():
        argv += ["-e", "%s=%s" % (key, value)]
    argv += ["executor", "python", "-c", textwrap.dedent(script)]
    return subprocess.run(argv, capture_output=True, text=True, timeout=900, check=False)


@pytest.mark.container_contract
@pytest.mark.skipif(
    not CONTAINER_CONTRACT,
    reason="Set TEST_CONTAINER_CONTRACT=1 to run container contract tests",
)
class TestDockerE2E:
    """Container-level e2e tests using docker-compose.e2e.yaml."""

    def test_harness_files_exist(self):
        """Both halves of the topology, so a moved file cannot silently disarm
        the suite again. The old version checked only the compose file -- the
        one path the module could not get wrong."""
        assert COMPOSE_FILE.exists(), "Compose file not found: %s" % COMPOSE_FILE
        assert DOCKERFILE_E2E.exists(), (
            "Executor image definition not found: %s. Every executor contract "
            "below skips without it, which is how this job stayed green for "
            "two months while running nothing." % DOCKERFILE_E2E
        )

    @pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON or "runnable")
    def test_sandbox_create_argv_always_carries_an_explicit_policy(self):
        """With confinement on, the sandbox argv names openshell AND a policy.

        A ``sandbox create`` without ``--policy`` inherits OpenShell's image
        default instead of the repository's fail-closed profile, i.e. the agent
        runs under guardrails nobody declared.
        """
        result = _run_in_executor(
            {"MAC_OPENSHELL_SANDBOX": "1"},
            """\
            import os, sys, pathlib
            sys.path.insert(0, '/app/src')
            os.environ['MAC_OPENSHELL_SANDBOX'] = '1'
            from mac import executor_sandbox as es

            assert es._openshell_enabled(), 'MAC_OPENSHELL_SANDBOX=1 must confine'

            workspace = pathlib.Path('/tmp/e2e-workspace')
            workspace.mkdir(parents=True, exist_ok=True)
            argv = es._build_sandbox_create_argv(
                'mac-e2e-sandbox',
                workspace,
                'task',
                ['python', '-m', 'mac.agent_command', '/tmp/cmd'],
                extra_create_argv=[],
            )
            assert argv[:3] == [es._openshell_bin(), 'sandbox', 'create'], argv[:3]
            assert '--policy' in argv, 'no --policy: OpenShell would apply its image default'
            policy = argv[argv.index('--policy') + 1]
            assert pathlib.Path(policy).is_file(), 'policy path does not exist: %s' % policy
            print('SANDBOX_ON: OK policy=%s' % policy)
            """,
        )
        assert result.returncode == 0, "executor exited %s:\n%s\n%s" % (
            result.returncode,
            result.stdout,
            result.stderr,
        )
        assert "SANDBOX_ON: OK" in result.stdout

    @pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON or "runnable")
    def test_sandbox_off_leaves_the_per_task_wrap_disabled(self):
        """MAC_OPENSHELL_SANDBOX=0 disables the per-task wrap."""
        result = _run_in_executor(
            {"MAC_OPENSHELL_SANDBOX": "0"},
            """\
            import os, sys
            sys.path.insert(0, '/app/src')
            os.environ['MAC_OPENSHELL_SANDBOX'] = '0'
            from mac import executor_sandbox as es
            assert not es._openshell_enabled(), 'MAC_OPENSHELL_SANDBOX=0 must not confine'
            os.environ['MAC_OPENSHELL_SANDBOX'] = '1'
            assert es._openshell_enabled(), 'the gate must be read at call time, not frozen at import'
            print('SANDBOX_OFF_FALLBACK: OK')
            """,
        )
        assert result.returncode == 0, "executor exited %s:\n%s\n%s" % (
            result.returncode,
            result.stdout,
            result.stderr,
        )
        assert "SANDBOX_OFF_FALLBACK: OK" in result.stdout

    @pytest.mark.skipif(bool(_SKIP_REASON), reason=_SKIP_REASON or "runnable")
    def test_ocsf_event_flows_to_mac_observability(self):
        """A denied-egress OCSF event translates to a mac observation, escalated to warning."""
        result = _run_in_executor(
            {"MAC_RELAY_OBSERVABILITY": "1"},
            """\
            import sys, importlib, os
            sys.path.insert(0, '/app/src')
            os.environ['MAC_RELAY_OBSERVABILITY'] = '1'
            sys.modules.pop('mac.relay_observability', None)
            ro = importlib.import_module('mac.relay_observability')
            event = {
                'class_uid': 4001, 'severity_id': 2, 'action': 'denied',
                'message': 'DENIED: egress blocked',
                'device': {'hostname': 'test-container'},
            }
            obs = ro.ocsf_to_observation(event)
            assert obs is not None, 'expected an observation'
            assert obs['layer'] == 'sandbox', 'layer=%s' % obs['layer']
            assert obs['source'] == 'openshell', 'source=%s' % obs['source']
            assert obs['level'] == 'warning', 'denied egress should escalate to warning, got %s' % obs['level']
            print('OCSF_FLOW: OK level=%s name=%s' % (obs['level'], obs['name']))
            """,
        )
        assert result.returncode == 0, "executor exited %s:\n%s\n%s" % (
            result.returncode,
            result.stdout,
            result.stderr,
        )
        assert "OCSF_FLOW: OK" in result.stdout
