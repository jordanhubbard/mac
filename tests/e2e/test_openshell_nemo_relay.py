"""Containerized full-stack e2e: OpenShell sandbox + NeMo Relay observability.

These are the Docker/Podman container tests that exercise the full integration
chain against the real built image:

  1. ``MAC_OPENSHELL_SANDBOX=1`` makes ``task_executor._maybe_wrap_openshell()``
     prepend the openshell binary to the agent argv (confined execution).
  2. ``MAC_OPENSHELL_SANDBOX=0`` leaves argv unchanged (unconfined fallback).
  3. An OpenShell OCSF event translates via
     ``relay_observability.ocsf_to_observation()`` into a mac observation, and a
     denied-egress decision is escalated to at least ``warning``.

The container topology (executor + OpenTelemetry collector) lives in
``docker-compose.e2e.yaml`` / ``Dockerfile.e2e`` / ``otelcol-config.yaml``.

Run them::

    TEST_DOCKER_E2E=1 pytest tests/e2e/test_openshell_nemo_relay.py -v -m docker_e2e

They are marked ``docker_e2e`` and skipped automatically unless
``TEST_DOCKER_E2E=1`` is set and ``docker compose`` is available, so the normal
contract suite stays hermetic. Unit-level coverage of the sandbox wrapper and
the OCSF translator lives in ``tests/test_openshell_sandbox.py`` and
``tests/test_relay_observability.py``; this file is the container-level seam.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

DOCKER_E2E = os.environ.get("TEST_DOCKER_E2E", "").strip() == "1"
COMPOSE_FILE = Path(__file__).parent / "docker-compose.e2e.yaml"


def _docker_available() -> bool:
    """Return True if docker/podman compose is functional."""
    try:
        r = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True, timeout=10, check=False,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


@pytest.mark.docker_e2e
@pytest.mark.skipif(not DOCKER_E2E, reason="Set TEST_DOCKER_E2E=1 to run Docker e2e tests")
class TestDockerE2E:
    """Container-level e2e tests using docker-compose.e2e.yaml."""

    def test_compose_file_exists(self):
        assert COMPOSE_FILE.exists(), f"Compose file not found: {COMPOSE_FILE}"

    @pytest.mark.skipif(not _docker_available(), reason="docker/podman compose not available")
    def test_executor_sandbox_on(self):
        """executor container with MAC_OPENSHELL_SANDBOX=1 wraps argv with openshell."""
        result = subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE_FILE),
                "run", "--rm", "-e", "MAC_OPENSHELL_SANDBOX=1", "executor",
                "python", "-c",
                textwrap.dedent("""\
                    import sys, os, unittest.mock as mock
                    sys.path.insert(0, '/app/src')
                    from mac import task_executor as te
                    os.environ['MAC_OPENSHELL_SANDBOX'] = '1'
                    argv = ['python', '-m', 'hermes_cli.main', 'chat', '--yolo']
                    with mock.patch('shutil.which', return_value='/usr/bin/openshell'):
                        wrapped = te._maybe_wrap_openshell(argv)
                    assert wrapped[0] == '/usr/bin/openshell', f'expected openshell first, got {wrapped[0]}'
                    print('SANDBOX_ON: OK', wrapped[0])
                    sys.exit(0)
                """),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, f"executor exited {result.returncode}:\n{result.stderr}"
        assert "SANDBOX_ON: OK" in result.stdout

    @pytest.mark.skipif(not _docker_available(), reason="docker/podman compose not available")
    def test_executor_sandbox_off_fallback(self):
        """executor container with MAC_OPENSHELL_SANDBOX=0 uses unconfined argv."""
        result = subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE_FILE),
                "run", "--rm", "-e", "MAC_OPENSHELL_SANDBOX=0", "executor",
                "python", "-c",
                textwrap.dedent("""\
                    import sys, os
                    sys.path.insert(0, '/app/src')
                    from mac import task_executor as te
                    os.environ['MAC_OPENSHELL_SANDBOX'] = '0'
                    argv = ['python', '-m', 'hermes_cli.main', 'chat', '--yolo']
                    result = te._maybe_wrap_openshell(argv)
                    assert result == argv, f'expected unchanged argv, got {result}'
                    print('SANDBOX_OFF_FALLBACK: OK')
                    sys.exit(0)
                """),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, f"executor exited {result.returncode}:\n{result.stderr}"
        assert "SANDBOX_OFF_FALLBACK: OK" in result.stdout

    @pytest.mark.skipif(not _docker_available(), reason="docker/podman compose not available")
    def test_ocsf_event_flows_to_mac_observability(self):
        """A denied-egress OCSF event translates to a mac observation, escalated to warning."""
        result = subprocess.run(
            [
                "docker", "compose", "-f", str(COMPOSE_FILE),
                "run", "--rm", "-e", "MAC_RELAY_OBSERVABILITY=1", "executor",
                "python", "-c",
                textwrap.dedent("""\
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
                    sys.exit(0)
                """),
            ],
            capture_output=True, text=True, timeout=60, check=False,
        )
        assert result.returncode == 0, f"executor exited {result.returncode}:\n{result.stderr}"
        assert "OCSF_FLOW: OK" in result.stdout
