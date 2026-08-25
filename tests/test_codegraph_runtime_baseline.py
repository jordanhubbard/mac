from __future__ import annotations

from pathlib import Path

from mac.worker import DEFAULT_COMMAND_INVENTORY_NAMES


ROOT = Path(__file__).resolve().parents[1]
MUTABLE_CODEGRAPH_INSTALL = (
    "curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh"
)
CODEGRAPH_IMAGE_INSTALL = (
    'tar -xzf "/tmp/mac-openshell-build-assets/codegraph-${codegraph_arch}.tgz" '
    '-C "$CG_HOME" --strip-components=1'
)


def test_codegraph_is_documented_as_agent_runtime_baseline():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    runtime_contract = (ROOT / "docs" / "repository-runtime-contract.md").read_text(
        encoding="utf-8"
    )
    agents_text = " ".join(agents.split())
    runtime_contract_text = " ".join(runtime_contract.split())

    assert "CodeGraph is a legitimate runtime assumption" in agents_text
    assert "CodeGraph is advisory analysis support, not an evidence gate" in agents_text
    assert "mac.codegraph_audit.v1" in agents_text
    assert "fails the deploy if CodeGraph cannot be prepared" in agents_text
    for term in ("APIs", "code behavior", "call relationships"):
        assert term in agents_text
    assert "run `codegraph init`" in agents_text
    assert "do not commit" in agents_text
    assert "CodeGraph is a legitimate baseline runtime assumption" in runtime_contract_text
    assert "CodeGraph is also an enforced evidence gate" in runtime_contract_text
    assert "mac.codegraph_audit.v1" in runtime_contract_text
    assert "fails the deploy if CodeGraph cannot be prepared" in runtime_contract_text
    for term in ("repository APIs", "code behavior", "call relationships"):
        assert term in runtime_contract_text


def test_codegraph_presence_and_behavior_have_basic_runtime_coverage():
    deploy = (
        (ROOT / "deploy" / "deploy-mac-fleet.sh").read_text(encoding="utf-8")
        + "\n"
        + (ROOT / "deploy" / "fleet-node-install.sh").read_text(encoding="utf-8")
    )
    containerfile = (ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile").read_text(
        encoding="utf-8"
    )
    reviewed_assets = (ROOT / "deploy" / "reviewed-tool-assets.sh").read_text(encoding="utf-8")

    assert "reviewed-tool-assets.sh" in deploy
    assert "mac_install_reviewed_codegraph" in reviewed_assets
    assert "mac_install_reviewed_codegraph" not in deploy
    assert 'MAC_REVIEWED_CODEGRAPH_VERSION="v1.5.0"' in reviewed_assets
    assert "mac_verify_reviewed_asset" in reviewed_assets
    assert MUTABLE_CODEGRAPH_INSTALL not in deploy
    assert CODEGRAPH_IMAGE_INSTALL in containerfile
    assert 'CG_HOME="/usr/local/lib/codegraph/versions/${CODEGRAPH_VERSION}"' in containerfile
    assert 'ln -sfn "$CG_HOME" /usr/local/lib/codegraph/current' in containerfile
    assert "chown -R root:root /usr/local/lib/codegraph /usr/local/bin/codegraph" in containerfile
    assert "chmod 0755 /usr/local/bin/codegraph" in containerfile
    assert "codegraph install --yes" in containerfile
    assert "reviewed CodeGraph bundle is missing; complete node onboarding" in deploy
    assert "onboarded CodeGraph version differs" in deploy
    assert "validate_typed_prerequisite_bundle" in deploy
    assert "install_codegraph_cli" in deploy
    assert 'initialize_codegraph_repository "$SRC_DIR"' in deploy
    assert "install_codegraph_cli || true" not in deploy
    assert 'initialize_codegraph_repository "$SRC_DIR" || true' not in deploy
    assert "codegraph init" in deploy
    assert "codegraph" in DEFAULT_COMMAND_INVENTORY_NAMES
