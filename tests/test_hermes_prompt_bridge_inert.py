"""verify_hermes_prompt_bridge() must not hard-fail once Hermes-the-agent is gone.

The vendored Hermes agent runtime (a separate coding-agent codebase this
bridge imported as `agent.prompt_builder`) was removed on 2026-08-17 --
every static worker runs OpenClaw now. src/mac/hermes_startup.py's own
startup-health check already treats the prompt bridge as inert (not
required, not present) absent an explicit MAC_HERMES_AGENT_DIR pointing at
a real checkout. This deploy-time verification step predated that and
still imported the now-permanently-absent module unconditionally, crashing
every --first-hub-bootstrap deploy at "installing Hermes agent from
upstream" with ModuleNotFoundError: No module named 'agent'.
"""

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
    env_lines = ["log() { printf '%s\\n' \"$*\" >&2; }"]
    if agent_dir is not None:
        env_lines.append(f"MAC_HERMES_AGENT_DIR={agent_dir!r}")
    if extra_env:
        env_lines.extend(f"{key}={value!r}" for key, value in extra_env.items())
    snippet = "\n".join(
        [
            *env_lines,
            _function("verify_hermes_prompt_bridge"),
            "verify_hermes_prompt_bridge",
            "echo REACHED_END",
        ]
    )
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, check=False)


def test_bridge_is_inert_with_no_agent_dir_configured() -> None:
    result = _run(agent_dir=None)
    assert result.returncode == 0, result.stderr
    assert "inert" in result.stderr
    assert "REACHED_END" in result.stdout


def test_bridge_is_inert_when_configured_dir_has_no_runtime(tmp_path: Path) -> None:
    empty_dir = tmp_path / "no-hermes-here"
    empty_dir.mkdir()
    result = _run(agent_dir=str(empty_dir))
    assert result.returncode == 0, result.stderr
    assert "inert" in result.stderr
    assert "REACHED_END" in result.stdout


def test_hermes_dir_fallback_also_treated_as_inert_when_absent() -> None:
    # HERMES_DIR ($MAC_HOME/hermes-agent) is the legacy path symbol -- kept
    # only for guarded backup/restore logic, deliberately never created
    # since the 2026-08-17 removal. It must fall back to the same inert
    # behavior, not a hard failure.
    result = _run(
        agent_dir=None,
        extra_env={"HERMES_DIR": "/nonexistent/hermes-agent-dir"},
    )
    assert result.returncode == 0, result.stderr
    assert "inert" in result.stderr
    assert "REACHED_END" in result.stdout
