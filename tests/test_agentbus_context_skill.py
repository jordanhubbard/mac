"""The bus skill must be reachable, correct, and honest about its own reach.

A skill that names a command the parser does not have sends an agent to a
usage error during an incident, so the same enforcement the CLI skill gets
applies here. Two further things are pinned because they are what make this
skill *arrive*:

* `AGENTS.md` points at it — the skills directory is only read because that
  table sends readers there;
* the executor policy, which is the text delivered into EVERY task sandbox
  including projects that have no `skills/` directory of their own, carries the
  same instruction. The skill file reaches agents in the mac repository; the
  policy is what reaches everyone else, and if that ever stops being true the
  claim in the skill becomes a lie.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_mac_cli_skill import _mentioned_commands, _mentioned_flags, _options_for

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "agentbus-context" / "SKILL.md"


@pytest.fixture(scope="module")
def text() -> str:
    assert SKILL.is_file(), "the AgentBus context skill is missing"
    return SKILL.read_text(encoding="utf-8")


def test_every_command_the_skill_names_exists(text):
    from tests.test_mac_cli_skill import _command_tree

    tree = _command_tree()
    missing = [
        "mac " + " ".join(parts) for parts in sorted(_mentioned_commands(text)) if parts not in tree
    ]
    assert not missing, "the skill names commands that do not exist: %s" % missing


def test_every_flag_the_skill_names_exists(text):
    wrong = [
        "mac %s %s" % (" ".join(path), flag)
        for path, flag in sorted(_mentioned_flags(text))
        if flag not in _options_for(path)
    ]
    assert not wrong, "the skill names flags that do not exist: %s" % wrong


def test_the_skill_names_the_terminal_verbs(text):
    for verb in ("git.pr_opened", "git.merged", "git.canonical_advanced"):
        assert verb in text, "the skill does not mention %s" % verb
    # tree identity, not commit identity -- the reason the verbs are useful.
    assert "tree_sha" in text


def test_agents_md_sends_the_reader_here():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    assert "skills/agentbus-context/SKILL.md" in agents


def test_the_executor_policy_carries_the_same_instruction_for_every_project():
    """The delivery path that does NOT depend on a repository having skills/."""
    policy = " ".join(
        (ROOT / "src" / "mac" / "executor-policy.txt").read_text(encoding="utf-8").split()
    )
    assert "AgentBus context" in policy
    assert "do not open a second pull request" in policy
