"""Per-repo sandbox egress rendering (ADR 0009 §2a).

The security-relevant assertions here are the ones about what is REFUSED.
``egress.hosts`` is derived from lockfiles and ``.npmrc`` in the repository
working tree, so every input in these tests is reachable by anyone who can open
a pull request against a repo the fleet builds.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mac.sandbox_egress import (
    REJECT_MALFORMED,
    REJECT_UNTRUSTED_DERIVED,
    TIER_DERIVED_REGISTRY,
    TIER_HUB_DECLARED,
    TRUSTED_REGISTRY_HOSTS,
    classify_egress_hosts,
    expand_policy_text,
    normalize_host,
    render_egress_block,
)

BINARIES = ["/usr/bin/node", "/usr/bin/python3"]
BASE_POLICY = """version: 1

network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: hub.example.com
        port: 8789
"""


# --- normalize_host: the YAML/exfiltration injection gate -------------------


@pytest.mark.parametrize(
    "value",
    [
        "registry.npmjs.org",
        "REGISTRY.NPMJS.ORG",
        "  registry.npmjs.org  ",
        "registry.npmjs.org.",
        "files.pythonhosted.org",
        "a.b.c.d.example.com",
    ],
)
def test_normalize_host_accepts_plain_hostnames(value):
    assert normalize_host(value) == value.strip().rstrip(".").lower()


@pytest.mark.parametrize(
    "value",
    [
        # Globs: legitimate in a hand-reviewed policy, never synthesized from
        # untrusted input.
        "**.evil.example",
        "*.evil.example",
        # Schemes / paths / userinfo / ports smuggle a second identity past a
        # host allowlist.
        "https://evil.example",
        "evil.example/path",
        "user@evil.example",
        "evil.example:443",
        # YAML injection: these reach a policy file built by concatenation.
        "evil.example\n  - host: attacker.example",
        "evil.example: {}",
        "evil.example #comment",
        "evil.example\ttab",
        "- host: evil.example",
        # Structurally invalid.
        "",
        "   ",
        "localhost",
        "-leading.example",
        "trailing-.example",
        "..",
        # IP literals bypass the DNS-name identity the policy reasons about.
        "10.0.0.1",
        "127.0.0.1",
        # Wrong types.
        None,
        123,
        ["registry.npmjs.org"],
        {"host": "registry.npmjs.org"},
    ],
)
def test_normalize_host_rejects_everything_that_is_not_a_hostname(value):
    assert normalize_host(value) is None


def test_normalize_host_rejects_overlong_names():
    assert normalize_host(("a" * 60 + ".") * 5 + "example.com") is None


# --- classification: derived is untrusted, declared is not -----------------


def test_derived_registry_host_is_granted_read_only():
    decision = classify_egress_hosts(derived=["registry.npmjs.org"])
    assert decision.granted_hosts == ["registry.npmjs.org"]
    assert decision.granted[0].tier == TIER_DERIVED_REGISTRY
    assert decision.granted[0].access == "read-only"
    assert not decision.rejected


def test_derived_host_outside_the_allowlist_is_refused():
    """The core exfiltration guard: a lockfile naming an attacker host must not
    become a sandbox egress grant."""
    decision = classify_egress_hosts(
        derived=["registry.npmjs.org", "evil.example", "exfil.attacker.test"]
    )
    assert decision.granted_hosts == ["registry.npmjs.org"]
    refused = {item.host: item.reason for item in decision.rejected}
    assert refused == {
        "evil.example": REJECT_UNTRUSTED_DERIVED,
        "exfil.attacker.test": REJECT_UNTRUSTED_DERIVED,
    }


def test_a_repo_cannot_grant_itself_egress_by_declaring_it_in_tree():
    """Repo content only ever reaches the `derived` tier. Even a host that would
    be legitimate as a hub declaration is refused when it arrives from the
    worktree."""
    decision = classify_egress_hosts(derived=["opensky-network.org"])
    assert decision.is_empty
    assert decision.rejected[0].reason == REJECT_UNTRUSTED_DERIVED


def test_hub_declared_host_is_granted():
    decision = classify_egress_hosts(declared=["opensky-network.org"])
    assert decision.granted_hosts == ["opensky-network.org"]
    assert decision.granted[0].tier == TIER_HUB_DECLARED
    assert decision.granted[0].access == "read-only"


def test_host_in_both_tiers_is_attributed_to_the_stronger_one():
    decision = classify_egress_hosts(
        derived=["registry.npmjs.org"], declared=["registry.npmjs.org"]
    )
    assert len(decision.granted) == 1
    assert decision.granted[0].tier == TIER_HUB_DECLARED


def test_malformed_hosts_are_refused_from_both_tiers():
    decision = classify_egress_hosts(derived=["**.evil.example"], declared=["also bad\nhost: x"])
    assert decision.is_empty
    assert {item.reason for item in decision.rejected} == {REJECT_MALFORMED}


def test_no_proposals_yields_an_empty_decision():
    decision = classify_egress_hosts()
    assert decision.is_empty
    assert not decision.rejected


def test_grants_are_sorted_and_deduplicated():
    decision = classify_egress_hosts(derived=["pypi.org", "registry.npmjs.org", "pypi.org"])
    assert decision.granted_hosts == ["pypi.org", "registry.npmjs.org"]


def test_rejection_echo_is_bounded():
    """A megabyte of lockfile garbage must not bloat the audit record."""
    decision = classify_egress_hosts(derived=["x" * 10_000])
    assert len(decision.rejected) == 1
    assert len(decision.rejected[0].host) <= 200


def test_duplicate_rejections_are_collapsed():
    decision = classify_egress_hosts(derived=["evil.example"] * 50)
    assert len(decision.rejected) == 1


def test_allowlist_is_overridable_for_fleets_with_a_private_registry():
    decision = classify_egress_hosts(
        derived=["npm.internal.example"],
        trusted_registries={"npm.internal.example"},
    )
    assert decision.granted_hosts == ["npm.internal.example"]


def test_default_allowlist_contains_only_wellformed_hosts():
    for host in TRUSTED_REGISTRY_HOSTS:
        assert normalize_host(host) == host


def test_decision_audit_shape_retains_rejections():
    """A repo whose legitimate dependency was denied and a repo probing for an
    exfiltration path are indistinguishable in a log that records only grants."""
    payload = classify_egress_hosts(derived=["registry.npmjs.org", "evil.example"]).to_dict()
    assert payload["granted_count"] == 1
    assert payload["rejected_count"] == 1
    assert payload["rejected"][0]["host"] == "evil.example"


# --- rendering -------------------------------------------------------------


def test_render_emits_a_wellformed_read_only_block():
    decision = classify_egress_hosts(derived=["registry.npmjs.org"])
    block = render_egress_block(decision, binaries=BINARIES)
    assert "  repo_declared_egress:" in block
    assert "      - host: registry.npmjs.org" in block
    assert "        access: read-only" in block
    assert "      - { path: /usr/bin/node }" in block
    assert "tier: %s" % TIER_DERIVED_REGISTRY in block


def test_render_of_an_empty_decision_is_empty():
    assert render_egress_block(classify_egress_hosts(), binaries=BINARIES) == ""


def test_render_requires_a_binary():
    decision = classify_egress_hosts(derived=["pypi.org"])
    with pytest.raises(ValueError):
        render_egress_block(decision, binaries=[])
    with pytest.raises(ValueError):
        render_egress_block(decision, binaries=["  "])


def test_rendered_policy_parses_as_yaml_and_only_adds_hosts():
    yaml = pytest.importorskip("yaml")
    decision = classify_egress_hosts(
        derived=["registry.npmjs.org"], declared=["opensky-network.org"]
    )
    expanded = expand_policy_text(BASE_POLICY, decision, binaries=BINARIES)
    parsed = yaml.safe_load(expanded)
    policies = parsed["network_policies"]
    # The base block survives untouched.
    assert policies["mac_hub"]["endpoints"][0]["host"] == "hub.example.com"
    added = policies["repo_declared_egress"]
    assert [e["host"] for e in added["endpoints"]] == [
        "opensky-network.org",
        "registry.npmjs.org",
    ]
    assert {e["access"] for e in added["endpoints"]} == {"read-only"}


def test_expansion_cannot_relax_the_base_posture():
    """Expansion appends; it never rewrites. Landlock, run_as_user and the
    filesystem rules must survive byte for byte."""
    yaml = pytest.importorskip("yaml")
    base = (
        BASE_POLICY
        + """
landlock:
  compatibility: hard_requirement

process:
  run_as_user: sandbox
"""
    )
    decision = classify_egress_hosts(derived=["pypi.org"])
    expanded = expand_policy_text(base, decision, binaries=BINARIES)
    assert expanded.startswith(base.rstrip("\n"))
    parsed = yaml.safe_load(expanded)
    assert parsed["landlock"]["compatibility"] == "hard_requirement"
    assert parsed["process"]["run_as_user"] == "sandbox"


def test_empty_decision_returns_base_policy_byte_for_byte():
    assert (
        expand_policy_text(BASE_POLICY, classify_egress_hosts(), binaries=BINARIES) is BASE_POLICY
    )


def test_expansion_refuses_a_policy_with_no_network_policies_key():
    """Appending under a missing mapping would produce a policy whose egress
    silently does nothing — worse than refusing."""
    decision = classify_egress_hosts(derived=["pypi.org"])
    with pytest.raises(ValueError, match="network_policies"):
        expand_policy_text("version: 1\n", decision, binaries=BINARIES)


def test_expansion_refuses_the_real_bundled_fail_closed_default():
    """Regression: the bundled default ends `network_policies: {}`, so a naive
    substring check accepts it and appending block entries under a FLOW mapping
    is a YAML parse error — every task on a default-configured host would fail.

    Refusing is also the right posture on the merits: widening the deny-all
    default would turn "unconfigured deployment fails closed" into
    "unconfigured deployment has egress".
    """
    from mac.executor_sandbox import _bundled_default_policy

    text = _bundled_default_policy().read_text(encoding="utf-8")
    assert "network_policies: {}" in text  # the shape this guards against
    decision = classify_egress_hosts(derived=["pypi.org"])
    with pytest.raises(ValueError, match="network_policies"):
        expand_policy_text(text, decision, binaries=BINARIES)


def test_expansion_of_the_real_operator_template_parses():
    """The other half of the same regression: the shipped operator template MUST
    expand into valid YAML, or the feature is unusable on a real fleet."""
    yaml = pytest.importorskip("yaml")
    from mac.openshell_policy import render_policy

    template = (
        Path(__file__).resolve().parents[1] / "deploy" / "openshell" / "mac-hermes-policy.yaml"
    )
    base = render_policy(
        template.read_text(encoding="utf-8"),
        agent_user="agent",
        hub_host="hub.example.com",
        hub_port=8789,
    )
    decision = classify_egress_hosts(derived=["registry.npmjs.org"])
    parsed = yaml.safe_load(expand_policy_text(base, decision, binaries=BINARIES))
    added = parsed["network_policies"]["repo_declared_egress"]
    assert [e["host"] for e in added["endpoints"]] == ["registry.npmjs.org"]
    # The template's own blocks survive.
    assert "mac_hub" in parsed["network_policies"]
    assert parsed["process"]["run_as_user"] == "sandbox"


def test_expansion_refuses_to_duplicate_an_existing_block_name():
    decision = classify_egress_hosts(derived=["pypi.org"])
    base = BASE_POLICY + "  repo_declared_egress:\n    name: preexisting\n"
    with pytest.raises(ValueError, match="already declares"):
        expand_policy_text(base, decision, binaries=BINARIES)


def test_expansion_refuses_an_unparseable_base_policy():
    decision = classify_egress_hosts(derived=["pypi.org"])
    with pytest.raises(ValueError, match="not valid YAML"):
        expand_policy_text("network_policies:\n  - [unbalanced\n", decision, binaries=BINARIES)


def test_injection_attempt_never_reaches_the_rendered_policy():
    """End-to-end: a hostile lockfile host must not appear in the output in any
    form, escaped or otherwise."""
    yaml = pytest.importorskip("yaml")
    hostile = "evil.example\n  attacker_block:\n    name: pwned"
    decision = classify_egress_hosts(derived=["registry.npmjs.org", hostile])
    expanded = expand_policy_text(BASE_POLICY, decision, binaries=BINARIES)
    assert "attacker_block" not in expanded
    assert "pwned" not in expanded
    assert set(yaml.safe_load(expanded)["network_policies"]) == {
        "mac_hub",
        "repo_declared_egress",
    }
