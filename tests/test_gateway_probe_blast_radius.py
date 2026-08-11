"""A chat-gateway failure must not stop task execution fleet-wide.

WHAT HAPPENED

On 2026-08-11 three consecutive deploys were blocked by the OpenClaw
gateway/channel health probe, on a different node each time. Each time the node
install failed, the cohort transaction treated the cohort as unproven, and all
eight agents were left drained and held. The fix they were waiting for sat in a
merged PR that could not be deployed because Slack would not answer.

WHY THAT IS WRONG

The OpenClaw gateway is the CONVERSATION surface. Task execution is OpenShell
plus the coding CLI plus mac-agent, and none of them consult Slack. A node that
cannot post to Slack is degraded for chat and fully capable of work.

The probe has a 90s budget and failed on a different node each run, which is
the signature of a timing budget rather than a broken host.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy" / "fleet-node-install.sh"


@pytest.fixture(scope="module")
def script() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_the_probe_is_non_fatal_by_default(script: str):
    """Default 0. An operator can still demand a proven gateway, but the
    default must not be "one node's Slack account can stop the fleet"."""
    assert 'MAC_DEPLOY_GATEWAY_PROBE_FATAL:-0' in script


def test_every_gateway_failure_consults_the_policy(script: str):
    """Each supervisor has its own path -- systemd, supervisord, launchd -- and
    a path that skips the check silently keeps the old blast radius on that
    platform only, which is the hardest kind of regression to notice."""
    for context in (
        "stock OpenClaw verification failed",
        "OpenClaw exclusivity proof failed",
        "stock OpenClaw verification failed under supervisord",
        "OpenClaw exclusivity proof failed under supervisord",
        "stock OpenClaw verification failed under launchd",
    ):
        index = script.find(context)
        assert index > 0, "missing failure path: %s" % context
        window = script[index : index + 900]
        assert "gateway_probe_is_fatal" in window, (
            "%s does not consult the fatality policy" % context
        )


def test_a_degraded_gateway_is_recorded_not_swallowed(script: str):
    """Continuing quietly would trade one bad failure mode for another: chat
    silently dead on a node with nothing to say why."""
    assert "note_gateway_degraded" in script
    assert "openclaw-gateway-degraded.txt" in script


def test_the_rollback_path_stays_fatal(script: str):
    """The one case that must NOT be downgraded. When the successor was
    withdrawn the gateway state is inconsistent, and installing an agent over a
    half-undone transaction is worse than failing the node."""
    index = script.find("mac_launchd_transaction_rollback")
    assert index > 0
    window = script[max(0, index - 700) : index]
    assert "gateway state is NOT" in window or "stays fatal" in window, (
        "the rollback path must document and keep its fatality"
    )


def test_the_agent_service_is_installed_after_a_degraded_gateway(script: str):
    """The point of the change. Under systemd the abort happened BEFORE
    install_linux_agent_service, so a Slack timeout meant the node never got
    the agent it was being deployed to update."""
    verify = script.find("stock OpenClaw verification failed")
    install = script.find("install_linux_agent_service", verify)
    assert install > verify, "the agent service install must follow the probe"
    between = script[verify:install]
    # No unconditional early return may sit between them.
    assert "gateway_probe_is_fatal" in between


def test_the_script_still_parses():
    """A shell edit that does not parse fails every deploy on every host."""
    import subprocess

    result = subprocess.run(
        ["bash", "-n", str(INSTALLER)], capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
