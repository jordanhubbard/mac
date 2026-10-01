"""The nvidia-inference-multimodal skill ships fleet-wide (no GPU gate) from
deploy/skills/fleet: vision + image generation route to the hub's hosted models
through the in-mac router, so every agent can use it without a local GPU."""

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
