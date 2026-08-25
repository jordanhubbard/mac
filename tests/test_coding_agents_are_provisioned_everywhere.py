"""Every coding agent mac will route to must exist in the sandbox and be allowed there.

A coding agent is only usable if THREE independent artifacts agree:

  1. src/mac/coding_agent.py  -- mac will route to it
  2. the Containerfile        -- the binary exists in the task image
  3. the OpenShell policy     -- the sandbox permits it to reach its provider

Nothing bound them together. opencode was added to (1) on 2026-08-19 and the
gap was invisible: the host detector reported it available, `opencode run`
worked on every node, and the fleet inventory still said available=False,
because `available` tracks the in-sandbox preflight -- which could never pass
against an image that had no opencode binary and a policy that granted it no
egress.

That failure is silent by construction. Each artifact is individually correct;
only the intersection is wrong. So the intersection is what these tests check.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mac.coding_agent import AGENT_PRIORITY
from mac.sandbox_bom import CODING_AGENT_SANDBOX_BINARY

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
POLICY = ROOT / "deploy" / "openshell" / "mac-hermes-policy.yaml"

#: The basename each agent must resolve to inside the sandbox, imported from
#: the module that already has to know it.
#:
#: This started as a copy here. That was a second place to update and therefore
#: a second place to forget -- the exact drift these tests exist to catch, in
#: the test that catches it. sandbox_bom needs the mapping anyway (the BOM lists
#: the binaries the image must carry) and raises on an agent it does not know,
#: so importing it means adding an agent fails in ONE place, loudly, instead of
#: passing here against a stale copy.
SANDBOX_BINARY = CODING_AGENT_SANDBOX_BINARY


def _policy() -> dict:
    return yaml.safe_load(POLICY.read_text(encoding="utf-8"))


@pytest.mark.parametrize("agent", AGENT_PRIORITY)
def test_every_routable_agent_is_installed_in_the_task_image(agent):
    """mac must not route to a binary the sandbox does not contain."""
    assert agent in SANDBOX_BINARY, (
        "%s is routable but this test does not know its sandbox binary name; "
        "add it here and to the image/policy" % agent
    )
    text = CONTAINERFILE.read_text(encoding="utf-8")
    binary = SANDBOX_BINARY[agent]
    assert "command -v %s" % binary in text, (
        "%s is in AGENT_PRIORITY but the Containerfile never gates on "
        "`command -v %s`; the in-sandbox preflight will fail with "
        "agent_binary_missing" % (agent, binary)
    )


@pytest.mark.parametrize("agent", AGENT_PRIORITY)
def test_every_routable_agent_has_provider_egress_in_the_policy(agent):
    """A binary with no egress is denied at the proxy, not at selection."""
    policies = _policy().get("network_policies") or {}
    key = "%s_provider" % agent
    assert key in policies, (
        "%s is in AGENT_PRIORITY but the OpenShell policy has no %s block; "
        "the sandbox will deny its provider egress" % (agent, key)
    )
    block = policies[key]
    assert block.get("endpoints"), "%s grants no endpoints" % key
    assert block.get("binaries"), (
        "%s grants endpoints but binds them to no binary, so the proxy cannot "
        "attribute the socket" % key
    )


@pytest.mark.parametrize("agent", AGENT_PRIORITY)
def test_policy_binaries_match_the_image_path(agent):
    """The policy must name the path the image actually installs.

    A policy entry for a path the image does not create silently grants
    nothing: OpenShell attributes sockets by resolved executable, so a stale
    path is indistinguishable from no rule at all.
    """
    policies = _policy().get("network_policies") or {}
    paths = [b["path"] for b in policies["%s_provider" % agent]["binaries"]]
    expected = "/usr/local/bin/%s" % SANDBOX_BINARY[agent]
    assert expected in paths, "%s policy lists %s but the image installs %s" % (
        agent,
        paths,
        expected,
    )


def test_the_binary_map_covers_exactly_the_routable_agents():
    """Adding an agent to mac forces a decision here, rather than a silent gap."""
    assert set(SANDBOX_BINARY) == set(AGENT_PRIORITY), (
        "SANDBOX_BINARY and AGENT_PRIORITY have diverged: %s"
        % (set(SANDBOX_BINARY) ^ set(AGENT_PRIORITY))
    )
