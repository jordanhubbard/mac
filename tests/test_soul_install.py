"""mac.soul_install puts the soul graph into a host's Hermes, idempotently."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from mac import soul_install
from mac.soul_graph import default_soul_path

PY = "/home/agent/.mac/venv/bin/python"

CONFIG = """\
model:
  provider: custom
  base_url: http://hub:8789/v1/
mcp_servers:
  codegraph:
    command: codegraph
    args:
      - serve
      - --mcp
    timeout: 120
    enabled: true
providers:
  custom:
    api: http://hub:8789/v1/
"""

SOUL = """\
# SOUL.md — Natasha

I'm **Natasha** — the sharp one.

## Core Truths

**Be genuinely helpful.** Skip the filler.

**Have opinions.** Disagree when it matters.
"""

MEMORY = "First memory about the fleet.\n§\nSecond memory: rocky is the hub.\n"


def _home(tmp_path: Path, config: str = CONFIG) -> Path:
    home = tmp_path / "hermes"
    (home / "memories").mkdir(parents=True)
    (home / "config.yaml").write_text(config)
    (home / "SOUL.md").write_text(SOUL)
    (home / "MEMORY.md").write_text(MEMORY)
    (home / "memories" / "MEMORY.md").write_text(MEMORY)  # Hermes keeps a copy
    return home


def test_install_adds_the_server_seeds_and_installs_the_skill(tmp_path: Path) -> None:
    home = _home(tmp_path)
    result = soul_install.install(home, PY)
    assert result["status"] == "ok"
    assert result["mcp_server"] == "added"
    assert result["seed"].startswith("seeded:")
    assert result["skill"] == "installed"

    config = yaml.safe_load((home / "config.yaml").read_text())
    assert set(config["mcp_servers"]) == {"codegraph", "soul"}
    assert config["mcp_servers"]["codegraph"]["args"] == ["serve", "--mcp"]
    soul = config["mcp_servers"]["soul"]
    assert soul["command"] == PY
    assert soul["args"] == ["-m", "mac.soul_mcp", "--soul-file", str(home / "soul.json")]
    assert soul["enabled"] is True
    assert config["providers"]["custom"]["api"] == "http://hub:8789/v1/"
    assert (home / "skills" / "soul-graph" / "SKILL.md").read_text() == soul_install.skill_text()
    assert list(home.glob("config.yaml.bak-*-pre-soul"))


def test_a_second_install_changes_nothing(tmp_path: Path) -> None:
    home = _home(tmp_path)
    soul_install.install(home, PY)
    config_before = (home / "config.yaml").read_text()
    soul_before = (home / "soul.json").read_text()
    backups = list(home.glob("config.yaml.bak-*"))

    result = soul_install.install(home, PY)

    assert result == {**result, "mcp_server": "unchanged", "seed": "exists", "skill": "unchanged"}
    assert (home / "config.yaml").read_text() == config_before
    assert (home / "soul.json").read_text() == soul_before
    assert list(home.glob("config.yaml.bak-*")) == backups


def test_a_moved_interpreter_updates_only_the_soul_entry(tmp_path: Path) -> None:
    home = _home(tmp_path)
    soul_install.install(home, PY)
    assert soul_install.ensure_mcp_server(home / "config.yaml", "/opt/py", home / "soul.json") == "updated"
    config = yaml.safe_load((home / "config.yaml").read_text())
    assert config["mcp_servers"]["soul"]["command"] == "/opt/py"
    assert config["mcp_servers"]["codegraph"]["command"] == "codegraph"
    assert (home / "config.yaml").read_text().count("  soul:") == 1


def test_a_config_without_mcp_servers_gets_the_section(tmp_path: Path) -> None:
    home = _home(tmp_path, config="model:\n  provider: custom\n")
    assert soul_install.ensure_mcp_server(home / "config.yaml", PY, home / "soul.json") == "added"
    config = yaml.safe_load((home / "config.yaml").read_text())
    assert config["model"] == {"provider": "custom"}
    assert list(config["mcp_servers"]) == ["soul"]


def test_seed_pins_identity_parents_entries_on_sections_and_dedupes_memory(tmp_path: Path) -> None:
    graph = soul_install.seed_graph(_home(tmp_path))
    by_content = {n.content: n for n in graph.nodes.values()}

    section = by_content["Core Truths"]
    assert section.pinned and {"soul", "section"} <= section.tags
    helpful = by_content["**Be genuinely helpful.** Skip the filler."]
    assert helpful.pinned and helpful.parents == [section.id]
    assert "SOUL.md — Natasha" not in by_content  # the title is not a node

    memories = [n for n in graph.nodes.values() if "memory" in n.tags]
    assert sorted(n.content for n in memories) == [
        "First memory about the fleet.",
        "Second memory: rocky is the hub.",
    ]
    assert not any(n.pinned for n in memories)


def test_an_existing_soul_file_is_never_replaced(tmp_path: Path) -> None:
    home = _home(tmp_path)
    (home / "soul.json").write_text('{"name": "soul", "nodes": {}}')
    assert soul_install.ensure_seeded(home, home / "soul.json") == "exists"
    assert json.loads((home / "soul.json").read_text()) == {"name": "soul", "nodes": {}}


def test_nothing_to_seed_leaves_no_file(tmp_path: Path) -> None:
    home = tmp_path / "bare"
    home.mkdir()
    assert soul_install.ensure_seeded(home, home / "soul.json") == "empty"
    assert not (home / "soul.json").exists()


def test_a_hand_written_skill_is_left_alone_but_an_old_managed_one_is_refreshed(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path)
    skill = home / "skills" / "soul-graph" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("my own notes\n")
    assert soul_install.ensure_skill(home).startswith("skipped")
    assert skill.read_text() == "my own notes\n"

    skill.write_text("old text <!-- %s -->\n" % soul_install.MANAGED_MARKER)
    assert soul_install.ensure_skill(home) == "installed"
    assert skill.read_text() == soul_install.skill_text()


def test_a_host_without_hermes_is_skipped(tmp_path: Path) -> None:
    result = soul_install.install(tmp_path / "nothing", PY)
    assert result["status"] == "skipped"
    assert not (tmp_path / "nothing").exists()


def test_the_skill_names_only_tools_the_server_has() -> None:
    import re

    from mac import soul_mcp

    served = {tool["name"] for tool in soul_mcp.SoulTools.TOOL_SPECS}
    named = set(re.findall(r"`(soul_[a-z]+)`", soul_install.skill_text()))
    assert named and named <= served


def test_the_default_soul_file_never_lands_in_an_openclaw_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.delenv("MAC_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert default_soul_path() == tmp_path / ".hermes" / "soul.json"

    monkeypatch.setenv("MAC_HOME", str(tmp_path / "mac"))
    assert default_soul_path() == tmp_path / "mac" / "hermes" / "soul.json"

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "h"))
    assert default_soul_path("other") == tmp_path / "h" / "other.json"


def test_main_reports_json(tmp_path: Path, capsys) -> None:
    home = _home(tmp_path)
    assert soul_install.main(["--hermes-home", str(home), "--python", PY]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok" and out["mcp_server"] == "added"
