"""The nvidia-inference-multimodal skill ships fleet-wide (no GPU gate) via the
deploy: vision + image generation route to the hub's hosted models through the
in-mac router, so every agent gets it without a local GPU. install_fleet_skills
copies deploy/skills/fleet/* into $MAC_HOME/openclaw/workspace/skills on every deploy."""

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


def test_fleet_skills_are_prepared_for_every_agent_before_typed_phase2():
    script = (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    fn = script.split("install_fleet_skills() {", 1)[1].split(
        "\ninstall_omniverse_gpu_skills() {", 1
    )[0]
    # fleet-wide: must NOT be GPU-gated, copies the deploy/skills/fleet assets
    assert "nvidia-smi" not in fn
    assert "deploy/skills/fleet" in fn
    assert '"$MAC_HOME/openclaw/workspace/skills"' in fn
    # The legacy/onboarding flow prepares the fleet-wide state before the
    # GPU-only assets. Typed phase 2 retains the prerequisite-proved state.
    legacy = script.split(
        'if [ "$NODE_ACTION" = legacy-one-shot ]; then\n  initialize_hermes_home', 1
    )[1].split('\nelse\n  log "typed phase 2 retained', 1)[0]
    assert (
        legacy.index("sync_hermes_chat_config")
        < legacy.index("apply_hermes_fleet_surface")
        < legacy.index("install_fleet_skills")
        < legacy.index("install_omniverse_gpu_skills")
    )
    assert "typed phase 2 retained the receipt-proved Hermes durable state" in script
