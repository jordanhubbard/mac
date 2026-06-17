"""Phase 3 — ACP capability builders + the well-known manifest (ADR 0006)."""

from __future__ import annotations

from mac.acp.capabilities import (
    MAC_EXTENSIONS,
    acp_manifest,
    mac_agent_capabilities,
    mac_client_capabilities,
    mac_meta,
)
from mac.acp.protocol import (
    PROTOCOL_VERSION,
    AgentCapabilities,
    ClientCapabilities,
)


def test_mac_meta_carries_the_three_extensions():
    meta = mac_meta()
    assert meta == {"mac": {"sandbox": True, "decomposition": True, "evidence": True}}
    # a fresh dict each call — mutating one must not leak into MAC_EXTENSIONS
    meta["mac"]["sandbox"] = False
    assert MAC_EXTENSIONS["sandbox"] is True


def test_client_capabilities_baseline_off_with_meta_extensions():
    caps = mac_client_capabilities()
    assert isinstance(caps, ClientCapabilities)
    wire = caps.to_dict()
    # mac is the host: it offers no fs/terminal helpers to the driven agent
    assert wire["fs"] == {"readTextFile": False, "writeTextFile": False}
    assert wire["terminal"] is False
    # the mac extensions ride under the vendor _meta key
    assert wire["_meta"] == {"mac": {"sandbox": True, "decomposition": True, "evidence": True}}


def test_agent_capabilities_carry_meta_extensions():
    caps = mac_agent_capabilities()
    assert isinstance(caps, AgentCapabilities)
    wire = caps.to_dict()
    assert wire["loadSession"] is False
    assert wire["_meta"]["mac"]["evidence"] is True


def test_capabilities_meta_roundtrips_through_from_dict():
    # additive: a peer that re-parses the wire form recovers the extensions
    parsed = ClientCapabilities.from_dict(mac_client_capabilities().to_dict())
    assert parsed.meta == {"mac": {"sandbox": True, "decomposition": True, "evidence": True}}
    parsed_agent = AgentCapabilities.from_dict(mac_agent_capabilities().to_dict())
    assert parsed_agent.meta == {"mac": {"sandbox": True, "decomposition": True, "evidence": True}}


def test_baseline_capabilities_omit_meta_for_interop():
    # an implementation that doesn't set _meta produces the unchanged baseline
    assert "_meta" not in ClientCapabilities().to_dict()
    assert "_meta" not in AgentCapabilities().to_dict()


def test_manifest_shape():
    manifest = acp_manifest()
    # protocolVersion is an integer MAJOR version (not a string)
    assert manifest["protocolVersion"] == PROTOCOL_VERSION
    assert isinstance(manifest["protocolVersion"], int)
    assert manifest["authMethods"] == []
    assert manifest["agentInfo"]["name"] == "mac"
    # agentCapabilities are the real mac agent capabilities
    assert manifest["agentCapabilities"]["loadSession"] is False
    # mac-specific extensions advertised at the top-level _meta
    assert manifest["_meta"] == {"mac": {"sandbox": True, "decomposition": True, "evidence": True}}
    assert manifest["agentCapabilities"]["_meta"]["mac"]["decomposition"] is True
