"""Tests for the OpenShell policy renderer (fills the operator template per fleet)."""

from __future__ import annotations

import re

import pytest
import yaml

from mac import openshell_policy as op

TEMPLATE = """version: 1
filesystem_policy:
  read_only:
    - /home/__AGENT_USER__/.mac/venv
network_policies:
  mac_hub:
    name: mac-hub
    endpoints:
      - host: __MAC_HUB_HOST__
        port: __MAC_HUB_PORT__
        protocol: rest
    binaries:
      - { path: /home/__AGENT_USER__/.mac/venv/bin/python }
  model_gateway:
    name: model-gateway
    endpoints:
      - host: __MODEL_GATEWAY_HOST__
        port: 443
"""


def test_render_substitutes_and_parses():
    out = op.render_policy(
        TEMPLATE,
        agent_user="jkh",
        hub_host="100.125.137.89",
        hub_port=8789,
        shared_services={"qdrant": 6333, "firecrawl": 3002},
    )
    assert "__" not in out  # no placeholder survives
    doc = yaml.safe_load(out)
    np = doc["network_policies"]
    assert np["mac_hub"]["endpoints"][0]["host"] == "100.125.137.89"
    assert np["mac_hub"]["endpoints"][0]["port"] == 8789
    # model gateway defaults to the hub host
    assert np["model_gateway"]["endpoints"][0]["host"] == "100.125.137.89"
    # shared-service blocks appended + valid
    assert np["qdrant"]["endpoints"][0]["port"] == 6333
    assert np["firecrawl"]["endpoints"][0]["host"] == "100.125.137.89"
    assert "/home/jkh/.mac/venv" in out


def test_explicit_model_gateway_host():
    out = op.render_policy(
        TEMPLATE,
        agent_user="u",
        hub_host="hubX",
        hub_port=8789,
        model_gateway_host="gw.example",
    )
    doc = yaml.safe_load(out)
    assert doc["network_policies"]["model_gateway"]["endpoints"][0]["host"] == "gw.example"


def test_unresolved_placeholder_in_active_config_raises():
    # an unresolved token in ACTIVE config (not a comment) -> fail closed
    bad = TEMPLATE + "\n  extra:\n    host: __MYSTERY_TOKEN__\n"
    with pytest.raises(ValueError, match="unresolved policy placeholders"):
        op.render_policy(bad, agent_user="u", hub_host="h", hub_port=8789)


def test_placeholder_in_comment_is_ignored():
    # template documentation comments legitimately keep __TOKENS__
    ok = TEMPLATE + "\n# see __PLACEHOLDER__ docs; optional __PYPI_MIRROR_HOST__\n"
    out = op.render_policy(ok, agent_user="u", hub_host="h", hub_port=8789)
    assert "__PLACEHOLDER__" in out  # preserved in the comment, did not raise


def test_missing_required_args_raise():
    with pytest.raises(ValueError):
        op.render_policy(TEMPLATE, agent_user="", hub_host="h", hub_port=8789)


def test_real_operator_template_renders(tmp_path):
    # The actual shipped template must render to placeholder-free valid YAML.
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    tmpl = (repo / "deploy" / "openshell" / "mac-hermes-policy.yaml").read_text(encoding="utf-8")
    # render_policy raising on any ACTIVE-config placeholder is the guarantee;
    # comments may still carry __TOKENS__ (template docs). Verify the parsed
    # config is correct.
    out = op.render_policy(
        tmpl,
        agent_user="jkh",
        hub_host="100.125.137.89",
        hub_port=8789,
        shared_services={"qdrant": 6333, "firecrawl": 3002},
    )
    doc = yaml.safe_load(out)
    assert doc["network_policies"]["mac_hub"]["endpoints"][0]["host"] == "100.125.137.89"
    assert doc["network_policies"]["mac_hub"]["endpoints"][0]["port"] == 8789
    assert doc["network_policies"]["qdrant"]["endpoints"][0]["port"] == 6333
    assert doc["network_policies"]["firecrawl"]["endpoints"][0]["host"] == "100.125.137.89"
    package_hosts = {
        endpoint["host"] for endpoint in doc["network_policies"]["python_packages"]["endpoints"]
    }
    assert {"pypi.org", "files.pythonhosted.org"} <= package_hosts
    github_bins = {binary["path"] for binary in doc["network_policies"]["github"]["binaries"]}
    assert {"/usr/bin/gh", "/usr/local/bin/gh"} <= github_bins
    claude_policy = doc["network_policies"]["claude_provider"]
    assert {endpoint["host"] for endpoint in claude_policy["endpoints"]} == {"api.anthropic.com"}
    cursor_policy = doc["network_policies"]["cursor_provider"]
    assert {
        "api2.cursor.sh",
        "**.api5.cursor.sh",
        "repo42.cursor.sh",
        "authenticator.cursor.sh",
    } <= {endpoint["host"] for endpoint in cursor_policy["endpoints"]}
    cursor_api5 = next(
        endpoint
        for endpoint in cursor_policy["endpoints"]
        if endpoint["host"] == "**.api5.cursor.sh"
    )
    assert cursor_api5 == {
        "host": "**.api5.cursor.sh",
        "port": 443,
        "tls": "skip",
    }
    containerfile = (repo / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )
    claude_version = re.search(
        r'^ARG CLAUDE_VERSION="([^"]+)"$', containerfile, re.MULTILINE
    ).group(1)
    cursor_version = re.search(
        r'^ARG CURSOR_VERSION="([^"]+)"$', containerfile, re.MULTILINE
    ).group(1)
    assert {
        "/usr/local/bin/claude",
        f"/usr/local/lib/claude-code/versions/{claude_version}/claude",
    } <= {binary["path"] for binary in claude_policy["binaries"]}
    assert {
        "/usr/local/bin/cursor-agent",
        f"/usr/local/lib/cursor-agent/versions/{cursor_version}/node",
    } <= {binary["path"] for binary in cursor_policy["binaries"]}
    # operator policy is best_effort (OpenShell egress-proxy incompatibility with
    # hard_requirement); the executor's Landlock precheck recovers fail-closed.
    assert doc["landlock"]["compatibility"] == "best_effort"
    assert "/home/jkh/.mac/venv" in out  # agent_user substituted in active config


def _real_template():
    from pathlib import Path

    repo = Path(__file__).resolve().parents[1]
    return (repo / "deploy" / "openshell" / "mac-hermes-policy.yaml").read_text(encoding="utf-8")


def test_dev_is_directory_not_leaf_under_hard_requirement():
    # /dev device-FILE leaves (/dev/null, /dev/urandom) given a directory access
    # right are rejected under hard_requirement on Landlock ABI >= 3; the policy
    # must list the /dev DIRECTORY instead (in either provisioning mode).
    for kwargs in ({}, {"image_runtime": "/opt/mac-venv"}):
        out = op.render_policy(
            _real_template(), agent_user="jkh", hub_host="h", hub_port=8789, **kwargs
        )
        doc = yaml.safe_load(out)
        fp = doc["filesystem_policy"]
        assert "/dev" in fp["read_write"]
        assert "/dev/null" not in fp["read_write"]
        assert "/dev/urandom" not in fp["read_only"]


def test_image_runtime_uses_in_image_paths_and_tmp_caches():
    out = op.render_policy(
        _real_template(),
        agent_user="jkh",
        hub_host="100.64.0.1",
        hub_port=8789,
        image_runtime="/opt/mac-venv",
        shared_services={"qdrant": 6333},
    )
    assert "__" not in "\n".join(l for l in out.splitlines() if not l.lstrip().startswith("#"))
    doc = yaml.safe_load(out)
    fp = doc["filesystem_policy"]
    assert "/opt/mac-venv" in fp["read_only"]
    assert not any("/.mac/venv" in p for p in fp["read_only"])  # not the host runtime
    # caches resolve to /tmp (already writable) — NOT nonexistent /tmp/.cache
    # leaves, which would break hard_requirement (ReadDir on an unclassifiable path)
    assert "/tmp" in fp["read_write"]
    assert not any(p.endswith("/.cache") or p.endswith("/.config") for p in fp["read_write"])
    # network binaries reference the in-image python, not the host path
    py = doc["network_policies"]["mac_hub"]["binaries"][0]["path"]
    assert py == "/opt/mac-venv/bin/python"
    assert doc["network_policies"]["qdrant"]["binaries"][0]["path"] == "/opt/mac-venv/bin/python"
    package_bins = {item["path"] for item in doc["network_policies"]["python_packages"]["binaries"]}
    assert {
        "/usr/local/bin/python3",
        "/usr/local/bin/python",
        "/usr/bin/python3",
        "/opt/mac-venv/bin/python",
    } <= package_bins
