"""The nvidia-inference-multimodal skill ships fleet-wide (no GPU gate) via the
deploy: vision + image generation route to the hub's hosted models through the
in-mac router, so every agent gets it without a local GPU. install_fleet_skills
copies deploy/skills/fleet/* into ~/.hermes/skills on every deploy."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "deploy" / "skills" / "fleet" / "nvidia-inference-multimodal" / "SKILL.md"


def test_multimodal_skill_present_and_well_formed():
    assert SKILL.exists(), "deploy/skills/fleet/nvidia-inference-multimodal/SKILL.md must exist"
    text = SKILL.read_text(encoding="utf-8")
    fm = yaml.safe_load(text.split("---\n", 2)[1])
    assert fm["name"] == "nvidia-inference-multimodal"
    assert "image" in fm["description"].lower() and "vision" in fm["description"].lower()
    # the verified recipes + key caveat
    assert "/v1/chat/completions" in text and "image_url" in text
    assert "/v1/genai/" in text
    assert "401" in text and "nvidia-image" in text


def test_fleet_skills_installed_for_every_agent():
    script = (
        (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    )
    fn = script.split("install_fleet_skills() {", 1)[1].split("\ninstall_omniverse_gpu_skills() {", 1)[0]
    # fleet-wide: must NOT be GPU-gated, copies the deploy/skills/fleet assets
    assert "nvidia-smi" not in fn
    assert 'deploy/skills/fleet' in fn
    assert '"$HOME/.hermes/skills"' in fn
    # invoked in the agent setup flow, before the GPU-only omniverse install
    assert "\nsync_hermes_chat_config\napply_hermes_fleet_surface\ninstall_fleet_skills\ninstall_omniverse_gpu_skills\n" in script
