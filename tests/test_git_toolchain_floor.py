from pathlib import Path

import yaml

from mac.services import _normalize_repository_contract


ROOT = Path(__file__).resolve().parents[1]


def test_git_minimum_and_provisioning_assets_are_guarded() -> None:
    contract = yaml.safe_load((ROOT / ".mac/project.yaml").read_text(encoding="utf-8"))
    minimum = tuple(map(int, contract["toolchain"]["minimum_versions"]["git"].split(".")))
    assert minimum >= (2, 38)

    for relative in (
        "Dockerfile",
        "Dockerfile.codex-runner",
        "deploy/openshell/mac-hermes.Containerfile",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "git" in text
        assert "v >= (2,38)" in text

    installer = (ROOT / "deploy/fleet-node-install.sh").read_text(encoding="utf-8")
    assert "verify_git_version" in installer
    assert "Git >= 2.38" in installer


def test_machine_onboarding_receipt_declares_git_floor() -> None:
    deploy = (ROOT / "deploy/deploy-mac-fleet.sh").read_text(encoding="utf-8")
    assert '"kind": "tool-version"' in deploy
    assert '"minimum_version": "2.38"' in deploy


def test_repository_contract_normalizes_minimum_versions() -> None:
    raw = yaml.safe_load((ROOT / ".mac/project.yaml").read_text(encoding="utf-8"))
    normalized = _normalize_repository_contract(raw, ".mac/project.yaml")
    assert normalized["toolchain"]["minimum_versions"] == {"git": "2.38"}
