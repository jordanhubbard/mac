from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "fleet-node-finalize.py"
GENERATION = "generation-123"
REVISION = "a" * 40
DEPLOY_TS = "20260719T010203Z"


@pytest.fixture
def finalizer() -> Any:
    spec = importlib.util.spec_from_file_location("fleet_node_finalize", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path: Path, value: dict[str, Any]) -> bytes:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(raw)
    path.chmod(0o600)
    return raw


def _generation(tmp_path: Path) -> Path:
    mac_home = tmp_path / ".mac"
    logs = mac_home / "logs"
    logs.mkdir(parents=True, mode=0o700)
    rollback_path = logs / f"rollback-{DEPLOY_TS}.sh"
    rollback_sha = "b" * 64
    intent_path = logs / f"rollback-{DEPLOY_TS}-intent.json"
    intent_raw = _write(
        intent_path,
        {
            "schema": "mac.fleet_node_rollback_intent.v1",
            "status": "armed",
            "generation": GENERATION,
            "revision": REVISION,
            "rollback": {"path": str(rollback_path), "sha256": rollback_sha},
        },
    )
    _write(
        logs / f"deploy-manifest-{DEPLOY_TS}-post.json",
        {
            "stage": "post",
            "deploy": {"generation": GENERATION, "mac_git_rev": REVISION},
            "rollback": {
                "schema": "mac.fleet_node_rollback_contract.v1",
                "status": "armed",
                "authority": "pre_mutation_intent",
                "path": str(rollback_path),
                "sha256": rollback_sha,
                "intent": {
                    "path": str(intent_path),
                    "sha256": __import__("hashlib").sha256(intent_raw).hexdigest(),
                },
            },
        },
    )
    return mac_home


def test_finalize_is_idempotent_and_generation_bound(finalizer: Any, tmp_path: Path) -> None:
    mac_home = _generation(tmp_path)
    first = finalizer.finalize(
        mac_home=mac_home,
        agent="worker-a",
        fleet="mac",
        generation=GENERATION,
        revision=REVISION,
        deploy_ts=DEPLOY_TS,
    )
    second = finalizer.finalize(
        mac_home=mac_home,
        agent="worker-a",
        fleet="mac",
        generation=GENERATION,
        revision=REVISION,
        deploy_ts=DEPLOY_TS,
    )
    assert first == second
    assert first["schema"] == "mac.fleet_node_finalize.v1"
    assert first["status"] == "finalized"
    output = mac_home / "logs" / f"deploy-{DEPLOY_TS}-finalize.json"
    assert output.stat().st_mode & 0o777 == 0o600


def test_finalize_rejects_different_generation(finalizer: Any, tmp_path: Path) -> None:
    mac_home = _generation(tmp_path)
    with pytest.raises(finalizer.FinalizeError, match="armed generation"):
        finalizer.finalize(
            mac_home=mac_home,
            agent="worker-a",
            fleet="mac",
            generation="different",
            revision=REVISION,
            deploy_ts=DEPLOY_TS,
        )


def test_finalize_rejects_hardlinked_input(finalizer: Any, tmp_path: Path) -> None:
    mac_home = _generation(tmp_path)
    post = mac_home / "logs" / f"deploy-manifest-{DEPLOY_TS}-post.json"
    os.link(post, tmp_path / "alias.json")
    with pytest.raises(finalizer.FinalizeError, match="owner-private"):
        finalizer.finalize(
            mac_home=mac_home,
            agent="worker-a",
            fleet="mac",
            generation=GENERATION,
            revision=REVISION,
            deploy_ts=DEPLOY_TS,
        )
