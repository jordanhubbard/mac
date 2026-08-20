"""Deploy-side OpenShell gating must be OS-aware (ADR 0015).

A darwin fleet node is a host install: there is no managed OpenShell container
runtime on it at all. Two deploy-orchestration decisions used to read "OpenShell
enabled" as if it meant the same thing on every OS, which made a macOS loop
worker both invisible to report-repository execution and permanently unready for
a Docker engine it will never have.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy" / "deploy-mac-fleet.sh"
BASE_CONFIG = ROOT / "deploy" / "fleet" / "config.yaml"

OS_FIELD = 2
WORKER_MODE_FIELD = 9
OPENSHELL_REQUIRED_FIELD = 53


def _script() -> str:
    return DEPLOY_SCRIPT.read_text(encoding="utf-8")


def _function(name: str) -> str:
    match = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(name),
        _script(),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, name
    return match.group(0)


def _heredoc_source(name: str) -> str:
    match = re.search(
        r"%s\(\) \{.*?<<'PY'\n(?P<source>.*?)\nPY\n\}" % re.escape(name),
        _script(),
        re.DOTALL,
    )
    assert match is not None, name
    return match.group("source")


def _spec(*, os_kind: str, worker_mode: str, openshell_required: str) -> str:
    fields = [""] * (OPENSHELL_REQUIRED_FIELD + 1)
    fields[0] = "node"
    fields[1] = "operator@node.example.internal"
    fields[OS_FIELD] = os_kind
    fields[WORKER_MODE_FIELD] = worker_mode
    fields[OPENSHELL_REQUIRED_FIELD] = openshell_required
    return "|".join(fields)


def _requires_report_executor(spec: str, *, deploy_openshell: str = "") -> bool:
    snippet = "\n".join(
        [
            _function("normalize_boolean_token"),
            _function("spec_requires_report_repository_executor"),
            'MAC_DEPLOY_OPENSHELL="$2"',
            'spec_requires_report_repository_executor "$1"',
        ]
    )
    result = subprocess.run(
        ["bash", "-c", snippet, "gating", spec, deploy_openshell],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode in {0, 1}, result.stderr
    return result.returncode == 0


def test_darwin_loop_worker_is_a_report_repository_executor() -> None:
    """The macos_host attestation qualifies it, not an OpenShell flag.

    On darwin OpenShell is always disabled, so gating on the flag benched every
    macOS loop worker from report-repository execution.
    """

    assert _requires_report_executor(
        _spec(os_kind="darwin", worker_mode="loop", openshell_required="0")
    )
    assert _requires_report_executor(
        _spec(os_kind="Darwin", worker_mode="loop", openshell_required="0")
    )


def test_darwin_non_loop_worker_is_still_not_a_report_executor() -> None:
    assert not _requires_report_executor(
        _spec(os_kind="darwin", worker_mode="heartbeat", openshell_required="0")
    )


def test_linux_gating_still_follows_the_frozen_openshell_deployment() -> None:
    assert not _requires_report_executor(
        _spec(os_kind="linux", worker_mode="loop", openshell_required="0")
    )
    assert _requires_report_executor(
        _spec(os_kind="linux", worker_mode="loop", openshell_required="1")
    )
    assert _requires_report_executor(
        _spec(os_kind="linux", worker_mode="loop", openshell_required="0"),
        deploy_openshell="1",
    )
    assert not _requires_report_executor(
        _spec(os_kind="linux", worker_mode="heartbeat", openshell_required="1")
    )


def _probe_openshell_runtime_required(*, expected_os: str, flag: str) -> bool:
    source = _heredoc_source("preflight_probe_helper_source")
    match = re.search(
        r"^openshell_runtime_required = \(\n.*?^\)$",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, "probe no longer derives openshell_runtime_required"
    namespace = {
        "truthy": lambda value: value.strip().lower() in {"1", "true", "yes", "on"},
        "expected_os": expected_os,
        "openshell_required": flag,
    }
    exec(match.group(0), namespace)  # noqa: S102 - the probe's own expression
    return bool(namespace["openshell_runtime_required"])


def test_darwin_node_never_requires_the_openshell_container_runtime() -> None:
    """A stale openshell_required=1 in the frozen spec must not bench a mac.

    The readiness probe used the shell-interpolated flag directly, so a darwin
    node whose fleet config still carried the container-era value failed
    ``openshell_container_runtime_if_required`` for missing Docker.
    """

    assert not _probe_openshell_runtime_required(expected_os="darwin", flag="1")
    assert not _probe_openshell_runtime_required(expected_os="Darwin", flag="true")
    assert _probe_openshell_runtime_required(expected_os="linux", flag="1")
    assert not _probe_openshell_runtime_required(expected_os="linux", flag="0")


def test_probe_readiness_check_uses_the_os_aware_value() -> None:
    source = _heredoc_source("preflight_probe_helper_source")
    assert (
        '"openshell_container_runtime_if_required": (not openshell_runtime_required)'
        " or docker_engine_ready," in source
    )
    assert "if openshell_runtime_required and docker_cli is not None:" in source
    # The OS-blind expression must not come back anywhere in the probe.
    assert "not truthy(openshell_required)" not in source
    assert source.count("truthy(openshell_required)") == 1


def _report_executor_approval_body() -> str:
    """The approval helper is a subshell function wrapping an SSH heredoc."""

    text = _script()
    start = text.index("reconcile_report_repository_executor_approval() (")
    end = text.index("\nREMOTE_REPORT_EXECUTOR_APPROVAL\n", start)
    return text[start:end]


def test_report_executor_approval_accepts_a_host_install_attestation() -> None:
    """The local pre-check must not be stricter than the hub approval it asks for."""

    function = _report_executor_approval_body()
    assert "report_repository_executor_attestation_is_host_install," in function
    assert (
        "or report_repository_executor_attestation_is_host_install(attestation)"
        in function
    )


def _fleet_config_query_source() -> str:
    return _heredoc_source("fleet_config_query")


def _specs(registry: Path, agent: str = "node") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-",
            "specs",
            str(BASE_CONFIG),
            str(registry),
            "hub",
            agent,
        ],
        input=_fleet_config_query_source(),
        text=True,
        capture_output=True,
        check=False,
    )


def _registry(tmp_path: Path, *, os_kind: str, worker_block: str, defaults: str = "") -> Path:
    registry = tmp_path / "fleets.yaml"
    registry.write_text(
        """
fleets:
  hub:
    sample: false
    fleet_name: default
    hub_agent: hub
    control_port: 8789
{defaults}    agents:
      hub:
        target: operator@hub.example.internal
        os: linux
      node:
        target: operator@node.example.internal
        os: {os_kind}
{worker_block}
""".lstrip().format(os_kind=os_kind, worker_block=worker_block, defaults=defaults),
        encoding="utf-8",
    )
    return registry


PURE_WORKER = """        hermes:
          gateway_impl: none
        worker:
          mode: loop
"""


def test_darwin_spec_never_freezes_openshell_required(tmp_path: Path) -> None:
    """A darwin pure worker would otherwise inherit the container-era default."""

    result = _specs(_registry(tmp_path, os_kind="darwin", worker_block=PURE_WORKER))
    assert result.returncode == 0, result.stderr
    fields = result.stdout.strip().split("|")
    assert fields[OS_FIELD] == "darwin"
    assert fields[WORKER_MODE_FIELD] == "loop"
    assert fields[OPENSHELL_REQUIRED_FIELD] == "0"


def test_linux_pure_worker_still_freezes_openshell_required(tmp_path: Path) -> None:
    result = _specs(_registry(tmp_path, os_kind="linux", worker_block=PURE_WORKER))
    assert result.returncode == 0, result.stderr
    fields = result.stdout.strip().split("|")
    assert fields[OPENSHELL_REQUIRED_FIELD] == "1"


@pytest.mark.parametrize("value", ["true", "yes", "on", "1"])
def test_darwin_agent_cannot_declare_openshell_required(tmp_path: Path, value: str) -> None:
    """The contradiction is rejected where it is written, not carried in a spec."""

    worker_block = "        worker:\n          openshell_required: %s\n" % value
    result = _specs(_registry(tmp_path, os_kind="darwin", worker_block=worker_block))
    assert result.returncode == 2
    assert "host install" in result.stderr
    assert "ADR 0015" in result.stderr


def test_fleet_wide_openshell_default_does_not_fail_a_darwin_node(tmp_path: Path) -> None:
    """A defaults.worker value is a fleet default, not a claim about this node."""

    defaults = """    defaults:
      worker:
        openshell_required: true
"""
    registry = _registry(
        tmp_path, os_kind="darwin", worker_block="", defaults=defaults
    )
    result = _specs(registry)
    assert result.returncode == 0, result.stderr
    fields = result.stdout.strip().split("|")
    assert fields[OPENSHELL_REQUIRED_FIELD] == "0"

    linux = _specs(
        _registry(tmp_path, os_kind="linux", worker_block="", defaults=defaults)
    )
    assert linux.returncode == 0, linux.stderr
    assert linux.stdout.strip().split("|")[OPENSHELL_REQUIRED_FIELD] == "1"
