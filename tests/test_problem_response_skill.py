from pathlib import Path

import yaml

from mac.harness_plugin import install


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "problem-response" / "SKILL.md"
BUNDLED_SKILL = (
    ROOT / "src" / "mac" / "data" / "hermes_skills" / "problem-response" / "SKILL.md"
)


def test_problem_response_skill_is_valid_and_bundled():
    text = SKILL.read_text(encoding="utf-8")
    frontmatter = yaml.safe_load(text.split("---\n", 2)[1])

    assert frontmatter["name"] == "problem-response"
    assert BUNDLED_SKILL.read_bytes() == SKILL.read_bytes()
    assert "## Step 1: Fix the problem" in text
    assert "## Step 2: Root cause analysis" in text
    assert "## Step 3: Root cause mitigation" in text


def test_problem_response_skill_is_deployed_by_plugin_install(tmp_path):
    plugin_root = tmp_path / "plugin"

    install(user_home=tmp_path, plugin_root=plugin_root, skills_root=ROOT / "skills")

    deployed = plugin_root / "skills" / "problem-response" / "SKILL.md"
    assert deployed.read_bytes() == SKILL.read_bytes()
