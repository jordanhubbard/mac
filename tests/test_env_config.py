"""Tests for the typed MAC_* config accessors (env-config-registry foundation)."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from mac.env_config import (
    ENV_VARS,
    EnvVar,
    MAC_API_ALLOW_OPEN,
    MAC_BEADS_BRIDGE_HUB_AGENT,
    environment_catalog,
    env_bool,
    env_int,
    env_str,
    resolve_env_chain,
    resolve_hub_agent,
)


def test_env_str():
    e = {"A": "  hi  ", "B": "  "}
    assert env_str("A", environ=e) == "hi"
    assert env_str("B", "def", environ=e) == "def"
    assert env_str("MISSING", "def", environ=e) == "def"


def test_env_bool():
    for token in ("1", "true", "TRUE", "yes", "on"):
        assert env_bool("F", environ={"F": token}) is True
    for token in ("0", "false", "no", "off"):
        assert env_bool("F", True, environ={"F": token}) is False
    assert env_bool("F", True, environ={}) is True          # unset -> default
    assert env_bool("F", True, environ={"F": "garbage"}) is True  # invalid -> default


def test_env_int_and_clamp():
    assert env_int("N", 5, environ={}) == 5
    assert env_int("N", 5, environ={"N": "12"}) == 12
    assert env_int("N", 5, environ={"N": "nope"}) == 5
    assert env_int("N", 5, minimum=0, maximum=10, environ={"N": "99"}) == 10
    assert env_int("N", 5, minimum=3, environ={"N": "1"}) == 3


def test_resolve_env_chain_priority():
    e = {"SECOND": "b", "THIRD": "c"}
    assert resolve_env_chain("FIRST", "SECOND", "THIRD", environ=e) == "b"
    assert resolve_env_chain("X", "Y", default="fallback", environ=e) == "fallback"
    # blank values are skipped
    assert resolve_env_chain("BLANK", "SECOND", environ={"BLANK": "  ", "SECOND": "b"}) == "b"


def test_resolve_hub_agent_ignores_removed_beads_var():
    # The removed beads subsystem's var must NOT resolve a hub agent.
    e = {"MAC_BEADS_BRIDGE_HUB_AGENT": "old-beads-agent"}
    assert resolve_hub_agent("MAC_SHARED_SERVICES_MANAGER_AGENT", environ=e) == ""
    assert resolve_hub_agent("MAC_REVIEW_TICK_HUB_AGENT", environ=e) == ""
    # A current var resolves normally.
    e2 = {"MAC_REVIEW_TICK_HUB_AGENT": "rocky"}
    assert resolve_hub_agent("MAC_REVIEW_TICK_HUB_AGENT", environ=e2) == "rocky"


def test_generated_registry_exports_typed_named_accessors():
    assert len(ENV_VARS) >= 200
    assert isinstance(MAC_API_ALLOW_OPEN, EnvVar)
    assert MAC_API_ALLOW_OPEN.kind == "bool"
    assert MAC_API_ALLOW_OPEN(environ={"MAC_API_ALLOW_OPEN": "yes"}) is True
    assert ENV_VARS["MAC_API_URL"](environ={"MAC_API_URL": " http://hub:8789 "}) == "http://hub:8789"
    assert environment_catalog() == sorted(environment_catalog(), key=lambda item: item.name)


def test_retired_registry_entry_is_documented_but_never_resolved():
    assert MAC_BEADS_BRIDGE_HUB_AGENT.retired is True
    assert (
        MAC_BEADS_BRIDGE_HUB_AGENT(environ={"MAC_BEADS_BRIDGE_HUB_AGENT": "stale"})
        is None
    )
    assert MAC_BEADS_BRIDGE_HUB_AGENT not in environment_catalog(include_retired=False)


def test_generated_registry_and_reference_are_current():
    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "scripts/generate-env-config-registry.py", "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def _load_env_config_generator():
    """Import the hyphenated generator script as a module for direct testing."""
    import importlib.util

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "generate-env-config-registry.py"
    spec = importlib.util.spec_from_file_location("env_config_generator", script)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_render_reference_documents_fleet_scoped_precedence():
    generator = _load_env_config_generator()
    reference = generator.render_reference([])

    # Fleet-scoped precedence section is present.
    assert "## Fleet-scoped credential precedence" in reference
    # Scoped naming rule: BASE_NAME__<FLEET>.
    assert "### Scoped naming rule" in reference
    assert "BASE_NAME__<FLEET>" in reference
    # Fleet-name normalization behavior is described.
    assert "normalized to an env-var suffix" in reference
    assert "uppercased" in reference
    # Scoped-wins-then-legacy-flat resolution order.
    assert "### Resolution order" in reference
    scoped_idx = reference.index("Scoped form wins.")
    legacy_idx = reference.index("Legacy flat form.")
    assert scoped_idx < legacy_idx
    # Deprecation / migration note.
    assert "deprecation warning" in reference
    assert "mac config migrate-env-namespace" in reference


def test_render_reference_lists_every_fleet_scoped_base_variable():
    from mac.fleet_env import FLEET_SCOPED_VARS

    generator = _load_env_config_generator()
    reference = generator.render_reference([])
    for base_name in FLEET_SCOPED_VARS:
        assert base_name in reference, base_name


def test_generated_reference_file_documents_fleet_scoped_vars():
    """The freshly generated docs must carry the fleet-scoped contract (drift guard)."""
    from mac.fleet_env import FLEET_SCOPED_VARS

    root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [sys.executable, "scripts/generate-env-config-registry.py", "--check"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr

    reference = (root / "docs" / "env-config-reference.md").read_text(encoding="utf-8")
    assert "## Fleet-scoped credential precedence" in reference
    assert "BASE_NAME__<FLEET>" in reference
    for base_name in FLEET_SCOPED_VARS:
        assert base_name in reference, base_name
