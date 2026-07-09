from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "provision-openclaw-personality.py"
SPEC = importlib.util.spec_from_file_location("openclaw_personality_provisioner", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _proposal(**updates):
    value = {
        "name": "Quill",
        "role": "Evidence cartographer",
        "vibe": "Precise and curious",
        "emoji": "quill",
        "soul": "A complete personality that maps claims to evidence.",
        "user": "Learn preferences rather than inventing them.",
        "memory": "Remember why this niche complements the fleet.",
        "rationale": "The fleet needs evidence mapping.",
    }
    value.update(updates)
    return value


def test_extracts_nested_openclaw_json_and_validates_unique_identity() -> None:
    output = json.dumps({"result": {"payloads": [{"text": json.dumps(_proposal())}]}})
    candidates = list(MODULE.json_candidates(output))
    validated = next(
        MODULE.validate_proposal(item, names={"rocky", "natasha"}, mentor="agent_rocky")
        for item in candidates
        if all(item.get(key) for key in MODULE.REQUIRED)
    )
    assert validated["schema"] == "mac.openclaw_personality_proposal.v1"
    assert validated["mentor_agent_id"] == "agent_rocky"
    assert validated["name"] == "Quill"


def test_rejects_duplicate_names_and_credential_material() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        MODULE.validate_proposal(_proposal(name="Rocky"), names={"rocky"}, mentor="agent_natasha")
    with pytest.raises(ValueError, match="credential"):
        MODULE.validate_proposal(
            _proposal(memory="github_pat_abcdefghijklmnopqrstuvwxyz123456"),
            names=set(),
            mentor="agent_natasha",
        )


def test_prompt_requires_complementary_non_overlapping_personality() -> None:
    prompt = MODULE.mentor_prompt(
        "new-worker",
        [{"id": "agent_rocky", "name": "rocky", "capabilities": ["ops"]}],
    )
    assert "Avoid duplicate names" in prompt
    assert "overlapping roles" in prompt
    assert "complete durable SOUL.md" in prompt
    assert "agent_rocky" in prompt
