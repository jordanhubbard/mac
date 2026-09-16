"""The required Hermes prompt bridge must resolve the active external runtime."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NODE_INSTALL_SCRIPT = ROOT / "deploy" / "fleet-node-install.sh"


def _function(name: str) -> str:
    match = re.search(
        r"^%s\(\) \{\n.*?^}$" % re.escape(name),
        NODE_INSTALL_SCRIPT.read_text(encoding="utf-8"),
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"function {name} not found"
    return match.group(0)


def _run(
    *, agent_dir: str | None, extra_env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    env_lines = [
        "log() { printf '%s\\n' \"$*\" >&2; }",
        'die() { log "ERROR: $*"; return 1; }',
    ]
    if agent_dir is not None:
        env_lines.append(f"MAC_HERMES_AGENT_DIR={agent_dir!r}")
    if extra_env:
        env_lines.extend(f"{key}={value!r}" for key, value in extra_env.items())
    snippet = "\n".join(
        [
            *env_lines,
            _function("verify_hermes_prompt_bridge"),
            "verify_hermes_prompt_bridge && echo REACHED_END",
        ]
    )
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)


def test_bridge_fails_when_no_active_runtime_is_configured() -> None:
    result = _run(agent_dir=None)
    assert result.returncode != 0
    assert "active Hermes runtime source is unavailable" in result.stderr
    assert "REACHED_END" not in result.stdout


def test_bridge_fails_when_configured_dir_has_no_runtime(tmp_path: Path) -> None:
    empty_dir = tmp_path / "no-hermes-here"
    empty_dir.mkdir()
    result = _run(agent_dir=str(empty_dir))
    assert result.returncode != 0
    assert "active Hermes runtime source is unavailable" in result.stderr
    assert "REACHED_END" not in result.stdout


def test_legacy_hermes_dir_is_not_accepted_as_runtime_evidence() -> None:
    result = _run(
        agent_dir=None,
        extra_env={"HERMES_DIR": "/nonexistent/hermes-agent-dir"},
    )
    assert result.returncode != 0
    assert "active Hermes runtime source is unavailable" in result.stderr
    assert "REACHED_END" not in result.stdout
