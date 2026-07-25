"""The fleet setup wizard must never persist a blank gateway_model — a blank
silently fell through to the router wildcard, which is how the fleet ran on
gpt-4.1-mini unnoticed. The picked model must be materialized into fleets.yaml."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load():
    path = Path(__file__).resolve().parents[1] / "scripts" / "setup-fleet.py"
    spec = importlib.util.spec_from_file_location("setup_fleet_script", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_resolve_gateway_model_materializes_default():
    m = _load()
    assert m.resolve_gateway_model("") == m.DEFAULT_GATEWAY_MODEL
    assert m.resolve_gateway_model("   ") == m.DEFAULT_GATEWAY_MODEL
    assert m.resolve_gateway_model("*") == m.DEFAULT_GATEWAY_MODEL
    assert m.resolve_gateway_model("custom/x") == "custom/x"
    # the default itself is a real, explicit model — not blank/wildcard
    assert m.DEFAULT_GATEWAY_MODEL and m.DEFAULT_GATEWAY_MODEL != "*"


def test_build_agent_never_writes_blank_model():
    m = _load()
    blank = m.build_agent(name="rocky", target="jkh@h", os_kind="linux", model="",
                          supervisor="systemd", mode="loop", claim_only_canary_tasks=False)
    assert blank["hermes"]["gateway_model"] == m.DEFAULT_GATEWAY_MODEL
    explicit = m.build_agent(name="x", target="t", os_kind="linux", model="custom/y",
                             supervisor="systemd", mode="loop", claim_only_canary_tasks=False)
    assert explicit["hermes"]["gateway_model"] == "custom/y"


def test_webdav_default_url_uses_https_dns_name():
    m = _load()
    assert m.webdav_url_from_dns("jordanhubbard.net") == "https://jordanhubbard.net/artifacts/"
    assert m.webdav_url_from_dns("jordanhubbard.net", "pub") == "https://jordanhubbard.net/pub/"
