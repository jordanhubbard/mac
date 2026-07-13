from __future__ import annotations

from pathlib import Path

from mac.worker import DEFAULT_COMMAND_INVENTORY_NAMES


ROOT = Path(__file__).resolve().parents[1]
CODEGRAPH_INSTALL = "curl -fsSL https://raw.githubusercontent.com/colbymchenry/codegraph/main/install.sh | sh"
CODEGRAPH_IMAGE_INSTALL = (
    "tar -xzf /tmp/mac-openshell-build-assets/codegraph.tgz "
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
    assert "CodeGraph is an enforced evidence gate" in agents_text
    assert "mac.codegraph_audit.v1" in agents_text
    assert "fails the deploy if CodeGraph cannot be prepared" in agents_text
    for term in ("APIs", "code behavior", "call relationships"):
        assert term in agents_text
    assert "run `codegraph init`" in agents_text
    assert "do not commit" in agents_text
    assert (
        "CodeGraph is a legitimate baseline runtime assumption" in runtime_contract_text
    )
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
    containerfile = (
        ROOT / "deploy" / "openshell" / "mac-hermes.Containerfile"
    ).read_text(encoding="utf-8")

    assert CODEGRAPH_INSTALL in deploy
    assert CODEGRAPH_IMAGE_INSTALL in containerfile
    assert (
        'CG_HOME="/usr/local/lib/codegraph/versions/${CODEGRAPH_VERSION}"'
        in containerfile
    )
    assert 'ln -sfn "$CG_HOME" /usr/local/lib/codegraph/current' in containerfile
    assert (
        "chown -R root:root /usr/local/lib/codegraph /usr/local/bin/codegraph"
        in containerfile
    )
    assert "chmod 0755 /usr/local/bin/codegraph" in containerfile
    assert "codegraph install --yes" in deploy
    assert "codegraph install --yes" in containerfile
    assert 'install_codegraph_cli\ninitialize_codegraph_repository "$SRC_DIR"' in deploy
    assert "install_codegraph_cli || true" not in deploy
    assert 'initialize_codegraph_repository "$SRC_DIR" || true' not in deploy
    assert "codegraph init" in deploy
    assert "codegraph" in DEFAULT_COMMAND_INVENTORY_NAMES
