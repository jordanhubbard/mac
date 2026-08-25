from __future__ import annotations

import argparse
import ast
import datetime as dt
import importlib.util
import json
from pathlib import Path
import stat
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepublish-fleet-qualification.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("prepublish_fleet_qualification", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _endpoint(index: int, *, hub: bool = False) -> dict:
    authority = {
        "ssh_host_key_sha256": str(index) * 64,
        "instance_id_kind": "machine-id",
        "instance_id_sha256": str(index + 1) * 64,
    }
    if hub:
        authority["durable_store_uuid_sha256"] = str(index + 2) * 64
    return {
        "schema": "mac.fleet_endpoint_identity.v1",
        "adapter": "ssh-hub" if hub else "ssh-machine",
        "authority": authority,
        "observation": {},
    }


def _agent(module, revision: str, name: str, index: int) -> dict:
    stable_id = f"agent_{name}"
    generation = f"generation-{index}"
    endpoint = _endpoint(index)
    reviewed = {
        "schema": "mac.reviewed_openshell_cli_preflight.v1",
        "status": "ready",
        "reason": "reviewed_cli_ready",
        "required": True,
        "managed_openclaw": True,
        "expected_os": "linux",
        "arch": "amd64",
        "version": "1.2.3",
        "asset": "openshell-linux-amd64.tar.gz",
        "asset_sha256": "3" * 64,
        "cli_sha256": "4" * 64,
        "receipt_sha256": "5" * 64,
    }
    probe = {
        "schema": "mac.fleet_preflight_node_probe.v1",
        "status": "passed",
        "read_only": True,
        "agent": name,
        "stable_id": stable_id,
        "generation": generation,
        "source_revision": revision,
        "platform": {"configured": "linux"},
        "checks": {"required_commands": True},
        "reviewed_openshell_cli": reviewed,
        "reviewed_openshell_cli_sha256": module.digest(module.canonical(reviewed)),
    }
    return {
        "name": name,
        "stable_id": stable_id,
        "generation": generation,
        "endpoint_identity": endpoint,
        "endpoint_identity_sha256": module.digest(module.canonical(endpoint)),
        "endpoint_identity_file_sha256": "6" * 64,
        "probe_evidence": probe,
        "probe_evidence_sha256": module.digest(module.canonical(probe)),
        "probe_evidence_file_sha256": "7" * 64,
    }


def _receipt(
    module,
    revision: str,
    registry_digest: str,
    *,
    qualified_at: dt.datetime | None = None,
) -> dict:
    hub_endpoint = _endpoint(6, hub=True)
    return {
        "schema": module.UPSTREAM_SCHEMA,
        "status": "passed",
        "read_only": True,
        "authorizes_deployment": False,
        "source_revision": revision,
        "fleet_name": "production",
        "hub_agent": "rocky",
        "fleet_registry_sha256": registry_digest,
        "selected_specs_sha256": "1" * 64,
        "probe_helper_sha256": "2" * 64,
        "hub_endpoint_identity": hub_endpoint,
        "hub_endpoint_identity_sha256": module.digest(module.canonical(hub_endpoint)),
        "hub_endpoint_identity_file_sha256": "8" * 64,
        "agents": [
            _agent(module, revision, "bullwinkle", 1),
            _agent(module, revision, "natasha", 2),
        ],
        "qualified_at": module.iso8601(qualified_at or module.utc_now()),
    }


def test_actual_agents_receipt_validates_canonical_embedded_evidence() -> None:
    module = _load_module()
    revision = "a" * 40
    registry_digest = "b" * 64
    value = _receipt(module, revision, registry_digest)

    validated = module.validate_upstream(
        value,
        revision=revision,
        hub="rocky",
        registry_sha256=registry_digest,
        requested_agents=["bullwinkle", "natasha"],
        max_age=900,
    )

    assert validated is value


@pytest.mark.parametrize(
    ("mutation", "error"),
    (
        (
            lambda value: value.update(
                qualified_at=(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2))
                .isoformat()
                .replace("+00:00", "Z")
            ),
            "stale",
        ),
        (
            lambda value: value["agents"][0]["probe_evidence"].update(status="failed"),
            "payload digest differs",
        ),
        (
            lambda value: value["agents"][1].update(
                endpoint_identity=value["agents"][0]["endpoint_identity"],
                endpoint_identity_sha256=value["agents"][0]["endpoint_identity_sha256"],
            ),
            "one physical endpoint",
        ),
    ),
)
def test_upstream_receipt_rejects_stale_tampered_or_aliased_nodes(mutation, error: str) -> None:
    module = _load_module()
    revision = "c" * 40
    registry_digest = "d" * 64
    value = _receipt(module, revision, registry_digest)
    mutation(value)

    with pytest.raises(module.QualificationError, match=error):
        module.validate_upstream(
            value,
            revision=revision,
            hub="rocky",
            registry_sha256=registry_digest,
            requested_agents=["bullwinkle", "natasha"],
            max_age=900,
        )


def test_private_receipt_reader_rejects_nonprivate_and_symlinked_files(
    tmp_path: Path,
) -> None:
    module = _load_module()
    receipt = tmp_path / "receipt.json"
    receipt.write_text("{}\n", encoding="utf-8")
    receipt.chmod(0o644)
    with pytest.raises(module.QualificationError, match="owner-private"):
        module.private_bytes(receipt, "qualification")

    receipt.chmod(0o600)
    link = tmp_path / "receipt-link.json"
    link.symlink_to(receipt)
    with pytest.raises(module.QualificationError, match="unreadable"):
        module.private_bytes(link, "qualification")


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_local_entrypoint_runs_exact_read_only_preflight_and_wraps_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    repository = tmp_path / "repository"
    deploy = repository / "deploy/deploy-mac-fleet.sh"
    deploy.parent.mkdir(parents=True)
    deploy.write_text(
        "#!/bin/sh\n"
        "set -eu\n"
        "receipt=''\n"
        'while [ "$#" -gt 0 ]; do\n'
        '  case "$1" in\n'
        "    --qualification-receipt) receipt=$2; shift 2 ;;\n"
        "    *) shift ;;\n"
        "  esac\n"
        "done\n"
        'test -n "$receipt"\n'
        'cp -f "$PREQUAL_TEST_RECEIPT" "$receipt"\n'
        'chmod 0600 "$receipt"\n'
        "printf 'read-only qualification passed\\n'\n",
        encoding="utf-8",
    )
    deploy.chmod(0o755)
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "ci@example.invalid")
    _git(repository, "config", "user.name", "CI")
    _git(repository, "add", "deploy/deploy-mac-fleet.sh")
    _git(repository, "commit", "-qm", "fixture")
    revision = _git(repository, "rev-parse", "HEAD")

    registry = tmp_path / "fleets.yaml"
    registry.write_text("fleets: {}\n", encoding="utf-8")
    registry.chmod(0o600)
    registry_digest = module.digest(registry.read_bytes())
    upstream = tmp_path / "upstream.json"
    upstream.write_text(
        json.dumps(_receipt(module, revision, registry_digest), sort_keys=True) + "\n",
        encoding="utf-8",
    )
    upstream.chmod(0o600)
    monkeypatch.setenv("PREQUAL_TEST_RECEIPT", str(upstream))

    output_dir = tmp_path / "private"
    output_dir.mkdir(mode=0o700)
    output = output_dir / "prepublication.json"
    args = argparse.Namespace(
        root=repository,
        deploy_script=None,
        fleets_config=registry,
        output=output,
        hub="rocky",
        agents=["bullwinkle", "natasha"],
        timeout=30,
        max_age=900,
    )
    receipt = module.run_qualification(args)

    assert receipt["schema"] == module.WRAPPER_SCHEMA
    assert receipt["status"] == "passed"
    assert receipt["read_only"] is True
    assert receipt["source_revision"] == revision
    assert [item["name"] for item in receipt["selected_agents"]] == [
        "bullwinkle",
        "natasha",
    ]
    assert receipt["qualification_payload_sha256"] == module.digest(
        module.canonical(receipt["qualification"])
    )
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert not (output_dir / ".prepublication.json.upstream").exists()


def test_documented_entrypoint_is_exact_and_owner_private() -> None:
    documentation = (ROOT / "docs/image-publication-and-qualification.md").read_text(
        encoding="utf-8"
    )
    assert "scripts/prepublish-fleet-qualification.py" in documentation
    assert "--preflight-only" in documentation
    assert "0600" in documentation
    assert "does not authorize deployment" in documentation


def test_entrypoint_avoids_python_310_only_zip_strict() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    zip_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "zip"
    ]
    assert zip_calls
    assert all(keyword.arg != "strict" for call in zip_calls for keyword in call.keywords)
