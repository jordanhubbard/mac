"""Tests for the OpenShell policy renderer (fills the operator template per fleet)."""

from __future__ import annotations

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
        TEMPLATE, agent_user="jkh", hub_host="100.125.137.89", hub_port=8789,
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
        TEMPLATE, agent_user="u", hub_host="hubX", hub_port=8789,
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
        tmpl, agent_user="jkh", hub_host="100.125.137.89", hub_port=8789,
        shared_services={"qdrant": 6333, "firecrawl": 3002},
    )
    doc = yaml.safe_load(out)
    assert doc["network_policies"]["mac_hub"]["endpoints"][0]["host"] == "100.125.137.89"
    assert doc["network_policies"]["mac_hub"]["endpoints"][0]["port"] == 8789
    assert doc["network_policies"]["qdrant"]["endpoints"][0]["port"] == 6333
    assert doc["network_policies"]["firecrawl"]["endpoints"][0]["host"] == "100.125.137.89"
    assert doc["landlock"]["compatibility"] == "hard_requirement"
    assert "/home/jkh/.mac/venv" in out  # agent_user substituted in active config
