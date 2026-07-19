from __future__ import annotations

import base64
import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "fleet-endpoint-identity.py"


@pytest.fixture
def identity_module() -> Any:
    spec = importlib.util.spec_from_file_location("fleet_endpoint_identity", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_cli(
    *args: str, check: bool = True
) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        text=True,
        capture_output=True,
        check=False,
    )
    payload = json.loads(result.stdout)
    if check:
        assert result.returncode == 0, payload
        assert payload["ok"] is True
    return result, payload


def write_private(path: Path, value: Any) -> Path:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def test_ssh_record_is_secret_free_deterministic_and_owner_private(
    tmp_path: Path,
) -> None:
    raw_fingerprint = bytes(range(32))
    openssh = "SHA256:" + base64.b64encode(raw_fingerprint).decode().rstrip("=")
    output = tmp_path / "identity.json"
    _result, payload = run_cli(
        "build-ssh",
        "--host-key-fingerprint",
        openssh,
        "--instance-id-kind",
        "linux-machine-id",
        "--instance-id",
        "AABBCCDD-0011",
        "--output",
        str(output),
    )
    identity = payload["identity"]
    assert identity == json.loads(output.read_text(encoding="utf-8"))
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert identity["adapter"] == "ssh-machine"
    assert identity["authority"]["ssh_host_key_sha256"] == raw_fingerprint.hex()
    serialized = output.read_text(encoding="utf-8")
    assert "AABBCCDD" not in serialized
    assert "aabbccdd" not in serialized
    assert "target" not in serialized
    assert "token" not in serialized


def test_ssh_recovery_requires_host_key_and_machine_identity(
    identity_module: Any,
) -> None:
    baseline = identity_module.build_ssh_machine(
        host_key_fingerprint="1" * 64,
        instance_id_kind="darwin-platform-uuid",
        instance_id="platform-a",
    )
    same = identity_module.build_ssh_machine(
        host_key_fingerprint="1" * 64,
        instance_id_kind="darwin-platform-uuid",
        instance_id="PLATFORM-A",
    )
    assert identity_module.compare_identities(baseline, same) == {
        "schema": "mac.fleet_endpoint_identity_comparison.v1",
        "adapter": "ssh-machine",
        "same_resource": True,
        "same_observation": True,
        "recovery_allowed": True,
        "generic_route_recovery_allowed": True,
        "requires_workload_adapter": False,
        "mismatches": [],
    }

    swapped_key = json.loads(json.dumps(same))
    swapped_key["authority"]["ssh_host_key_sha256"] = "2" * 64
    comparison = identity_module.compare_identities(baseline, swapped_key)
    assert comparison["recovery_allowed"] is False
    assert comparison["mismatches"] == ["authority.ssh_host_key_sha256"]

    swapped_machine = identity_module.build_ssh_machine(
        host_key_fingerprint="1" * 64,
        instance_id_kind="darwin-platform-uuid",
        instance_id="platform-b",
    )
    comparison = identity_module.compare_identities(baseline, swapped_machine)
    assert comparison["recovery_allowed"] is False
    assert comparison["mismatches"] == ["authority.instance_id_sha256"]


def test_kubernetes_authority_survives_pod_rollover_but_requires_adapter(
    identity_module: Any,
) -> None:
    before = identity_module.build_kubernetes_workload(
        cluster_uid="cluster-a",
        workload_kind="statefulset",
        workload_uid="workload-a",
        pod_uid="pod-old",
    )
    after = identity_module.build_kubernetes_workload(
        cluster_uid="CLUSTER-A",
        workload_kind="statefulset",
        workload_uid="WORKLOAD-A",
        pod_uid="pod-new",
    )
    comparison = identity_module.compare_identities(before, after)
    assert comparison["same_resource"] is True
    assert comparison["same_observation"] is False
    assert comparison["recovery_allowed"] is True
    assert comparison["generic_route_recovery_allowed"] is False
    assert comparison["requires_workload_adapter"] is True
    assert comparison["mismatches"] == ["observation.pod_uid_sha256"]

    wrong_cluster = identity_module.build_kubernetes_workload(
        cluster_uid="cluster-b",
        workload_kind="statefulset",
        workload_uid="workload-a",
        pod_uid="pod-new",
    )
    comparison = identity_module.compare_identities(before, wrong_cluster)
    assert comparison["recovery_allowed"] is False
    assert comparison["mismatches"] == ["authority.cluster_uid_sha256"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value.update({"target": "operator@example.invalid"}),
        lambda value: value["authority"].update({"token": "secret"}),
        lambda value: value.update({"schema": "mac.unknown.v1"}),
        lambda value: value["authority"].update({"instance_id_sha256": "short"}),
    ],
)
def test_schema_rejects_extra_sensitive_or_malformed_fields(
    identity_module: Any, mutation
) -> None:
    value = identity_module.build_ssh_machine(
        host_key_fingerprint="3" * 64,
        instance_id_kind="linux-machine-id",
        instance_id="machine-a",
    )
    mutation(value)
    with pytest.raises(identity_module.IdentityError):
        identity_module.validate_identity(value)


@pytest.mark.parametrize("attack", ["mode", "symlink", "oversize"])
def test_cli_refuses_unsafe_identity_files(tmp_path: Path, attack: str) -> None:
    _, built = run_cli(
        "build-ssh",
        "--host-key-fingerprint",
        "4" * 64,
        "--instance-id-kind",
        "linux-machine-id",
        "--instance-id",
        "machine-a",
        "--output",
        str(tmp_path / "safe.json"),
    )
    safe = tmp_path / "safe.json"
    candidate = safe
    if attack == "mode":
        safe.chmod(0o644)
    elif attack == "symlink":
        candidate = tmp_path / "link.json"
        candidate.symlink_to(safe)
    else:
        candidate = tmp_path / "large.json"
        candidate.write_bytes(b"{" + b" " * (64 * 1024) + b"}")
        candidate.chmod(0o600)
    result, payload = run_cli("validate", str(candidate), check=False)
    assert result.returncode == 2
    assert payload["ok"] is False
    assert built["identity"]["schema"] == "mac.fleet_endpoint_identity.v1"


def test_compare_refuses_cross_adapter_substitution(identity_module: Any) -> None:
    ssh = identity_module.build_ssh_machine(
        host_key_fingerprint="5" * 64,
        instance_id_kind="cloud-instance-id",
        instance_id="vm-a",
    )
    k8s = identity_module.build_kubernetes_workload(
        cluster_uid="cluster-a",
        workload_kind="deployment",
        workload_uid="workload-a",
        pod_uid="pod-a",
    )
    comparison = identity_module.compare_identities(ssh, k8s)
    assert comparison["same_resource"] is False
    assert comparison["recovery_allowed"] is False
    assert comparison["mismatches"] == ["adapter"]


def test_hub_identity_adds_durable_store_authority(identity_module: Any) -> None:
    first = identity_module.build_ssh_machine(
        host_key_fingerprint="6" * 64,
        instance_id_kind="linux-machine-id",
        instance_id="machine-a",
        durable_store_uuid="STORE-UUID-A",
    )
    same = identity_module.build_ssh_machine(
        host_key_fingerprint="6" * 64,
        instance_id_kind="linux-machine-id",
        instance_id="MACHINE-A",
        durable_store_uuid="store-uuid-a",
    )
    assert first["adapter"] == "ssh-hub"
    assert identity_module.compare_identities(first, same)["recovery_allowed"] is True
    different_store = identity_module.build_ssh_machine(
        host_key_fingerprint="6" * 64,
        instance_id_kind="linux-machine-id",
        instance_id="machine-a",
        durable_store_uuid="store-uuid-b",
    )
    comparison = identity_module.compare_identities(first, different_store)
    assert comparison["recovery_allowed"] is False
    assert comparison["mismatches"] == ["authority.durable_store_uuid_sha256"]


def test_case_sensitive_provider_instance_ids_are_not_folded(
    identity_module: Any,
) -> None:
    lower = identity_module.build_ssh_machine(
        host_key_fingerprint="7" * 64,
        instance_id_kind="cloud-instance-id",
        instance_id="Provider-ID-a",
    )
    upper = identity_module.build_ssh_machine(
        host_key_fingerprint="7" * 64,
        instance_id_kind="cloud-instance-id",
        instance_id="Provider-ID-A",
    )
    assert identity_module.compare_identities(lower, upper)["recovery_allowed"] is False
