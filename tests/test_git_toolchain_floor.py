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
        "Dockerfile.task-runner",
        "deploy/openshell/mac-hermes.Containerfile",
    ):
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert "git" in text
        assert "v >= (2,38)" in text


def test_repository_contract_normalizes_minimum_versions() -> None:
    raw = yaml.safe_load((ROOT / ".mac/project.yaml").read_text(encoding="utf-8"))
    normalized = _normalize_repository_contract(raw, ".mac/project.yaml")
    assert normalized["toolchain"]["minimum_versions"] == {"git": "2.38"}
