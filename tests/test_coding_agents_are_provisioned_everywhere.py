"""The coding CLI mac routes to must exist in the sandbox and be allowed there.

The route is only usable if THREE independent artifacts agree:

  1. src/mac/coding_agent.py  -- mac routes to opencode
  2. the Containerfile        -- the binary exists in the task image
  3. the OpenShell policy     -- the sandbox permits it to reach the hub router

Each artifact can be individually correct while the intersection is wrong
(opencode was once routable for weeks with no binary in the image), so the
intersection is what these tests check.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from mac.coding_agent import CODING_AGENT
from mac.sandbox_bom import MAC_CORE_COMMANDS

ROOT = Path(__file__).resolve().parents[1]
CONTAINERFILE = ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
POLICY = ROOT / "deploy" / "openshell" / "mac-hermes-policy.yaml"


def _policies() -> dict:
    return yaml.safe_load(POLICY.read_text(encoding="utf-8")).get("network_policies") or {}


def test_the_coding_cli_is_installed_in_the_task_image():
    text = CONTAINERFILE.read_text(encoding="utf-8")
    assert "command -v %s" % CODING_AGENT in text


def test_the_coding_cli_is_a_core_bom_command():
    assert CODING_AGENT in MAC_CORE_COMMANDS


def test_the_coding_cli_may_reach_the_hub_router_from_the_image_path():
    block = _policies()["%s_router" % CODING_AGENT]
    assert block.get("endpoints"), "the router block grants no endpoints"
    paths = [entry["path"] for entry in block.get("binaries") or []]
    assert "/usr/local/bin/%s" % CODING_AGENT in paths


def test_the_committed_bom_names_no_other_coding_cli():
    import json

    bom = json.loads((ROOT / "deploy" / "openshell" / "sandbox-bom.json").read_text("utf-8"))
    for key in ("commands", "core_commands"):
        assert CODING_AGENT in bom[key]
        assert not {"claude", "codex", "cursor-agent", "pi"} & set(bom[key]), key
