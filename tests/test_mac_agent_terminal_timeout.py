"""The mac-agent-terminal-timeout skill teaches fleet agents to pass an explicit
`terminal()` timeout for long MAC-repo operations (contract tests, bootstrap,
large git) and documents the CARGO_HOME/Rust toolchain pitfall where
`cargo` is not on PATH inside the task sandbox until `mac_sandbox_toolchain_setup`
symlinks it into MAC_TOOLCHAIN_BIN. These guards fail loudly if the skill is
removed, malformed, or loses the Rust-toolchain diagnostic guidance a blocked
agent relies on to recover."""

from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "mac-agent-terminal-timeout" / "SKILL.md"


def _front_matter(text: str) -> dict:
    parts = text.split("---\n", 2)
    assert len(parts) == 3, "SKILL.md must open with a '---' fenced YAML front-matter block"
    return yaml.safe_load(parts[1])


def test_terminal_timeout_skill_present_and_well_formed():
    assert SKILL.exists(), "skills/mac-agent-terminal-timeout/SKILL.md must exist"
    text = SKILL.read_text(encoding="utf-8")
    fm = _front_matter(text)
    assert fm["name"] == "mac-agent-terminal-timeout"
    assert isinstance(fm.get("description"), str) and fm["description"].strip()
    description = fm["description"].lower()
    assert "timeout" in description
    # version must be present and parseable as a dotted release string
    version = str(fm["version"]).strip()
    assert version and all(part.isdigit() for part in version.split(".")), (
        f"version front-matter must be a dotted numeric release, got {version!r}"
    )
    # platforms must be a non-empty list covering the fleet's OSes
    platforms = fm["platforms"]
    assert isinstance(platforms, list) and platforms, "platforms must be a non-empty list"
    assert {"linux", "macos"}.issubset({str(p).lower() for p in platforms})


def test_terminal_timeout_skill_documents_the_timeout_fix():
    text = SKILL.read_text(encoding="utf-8")
    # The core remedy: an explicit terminal() timeout for the slow operations.
    assert "terminal:timeout" in text
    assert "scripts/run-contract-tests.sh" in text
    assert "timeout=600" in text
    # Recovery guidance still points at the environment_delta evidence record.
    assert "environment_delta" in text


def test_terminal_timeout_skill_carries_cargo_home_pitfall():
    text = SKILL.read_text(encoding="utf-8")
    required_keywords = [
        "CARGO_HOME",
        "cargo/bin",
        "rustup",
        "rust-toolchain",
        "MAC_TOOLCHAIN_BIN",
        "mac_sandbox_toolchain_setup",
    ]
    missing = [kw for kw in required_keywords if kw not in text]
    assert not missing, (
        "mac-agent-terminal-timeout SKILL.md lost required CARGO_HOME/Rust "
        f"toolchain diagnostic keyword(s): {missing}"
    )
    # The pitfall must read as an actionable checklist, not just prose.
    assert "Fix checklist:" in text
    assert "required_commands" in text
