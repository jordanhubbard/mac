from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
import os
import platform
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "deploy" / "openshell" / "reviewed-cli.py"
ASSETS = ROOT / "deploy" / "openshell" / "reviewed-cli-assets.sh"
CONTROLLER = ROOT / "deploy" / "deploy-mac-fleet.sh"
BOOTSTRAP = ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh"
INSTALLER = ROOT / "deploy" / "fleet-node-install.sh"


def _module():
    spec = importlib.util.spec_from_file_location("reviewed_openshell_cli", HELPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _host_values() -> tuple[str, str, str, str, str]:
    os_kind = platform.system().lower()
    arch = platform.machine().lower()
    canonical_arch = {"arm64": "aarch64", "amd64": "x86_64"}.get(arch, arch)
    asset = f"openshell-{canonical_arch}-test.tar.gz"
    digest = "a" * 64
    cli_digest = "b" * 64
    return os_kind, canonical_arch, asset, digest, cli_digest


def _args(mac_home: Path, cli_digest: str | None = None) -> argparse.Namespace:
    os_kind, arch, asset, digest, default_cli_digest = _host_values()
    return argparse.Namespace(
        action="preflight",
        mac_home=str(mac_home),
        expected_os=os_kind,
        version="0.0.72",
        base_url="https://github.com/NVIDIA/OpenShell/releases/download/v0.0.72",
        asset_spec=[
            f"{os_kind}:{arch}:{asset}:{digest}:{cli_digest or default_cli_digest}"
        ],
        archive=None,
        required=False,
    )


def _managed_legacy_home(tmp_path: Path) -> Path:
    mac_home = tmp_path / ".mac"
    managed = mac_home / "openclaw" / "managed"
    managed.mkdir(parents=True, mode=0o700)
    runtime = managed / "runtime.env"
    runtime.write_text("MAC_OPENCLAW_SANDBOX=mac-openclaw-natasha\n", encoding="utf-8")
    runtime.chmod(0o600)
    return mac_home


def _reviewed_archive(tmp_path: Path) -> tuple[Path, str, str]:
    archive = tmp_path / "openshell-reviewed.tar.gz"
    payload = b"#!/bin/sh\necho openshell 0.0.72\n"
    member = tarfile.TarInfo("release/bin/openshell")
    member.size = len(payload)
    member.mode = 0o755
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.addfile(member, io.BytesIO(payload))
    archive.chmod(0o600)
    return (
        archive,
        hashlib.sha256(archive.read_bytes()).hexdigest(),
        hashlib.sha256(payload).hexdigest(),
    )


def test_pre_july_legacy_layout_is_classified_before_migration(tmp_path: Path) -> None:
    module = _module()
    mac_home = _managed_legacy_home(tmp_path)

    result = module.preflight(_args(mac_home))

    assert result["managed_openclaw"] is True
    assert result["status"] == "migration_required"
    assert result["reason"] == "canonical_directory_missing"


def test_publish_is_idempotent_owner_private_and_receipt_bound(tmp_path: Path) -> None:
    module = _module()
    mac_home = _managed_legacy_home(tmp_path)
    source = tmp_path / "openshell"
    source.write_bytes(b"#!/bin/sh\nexit 0\n")
    source.chmod(0o700)
    args = _args(mac_home, hashlib.sha256(source.read_bytes()).hexdigest())

    module.atomic_publish(args, source)
    first = module.preflight(args)
    module.atomic_publish(args, source)
    second = module.preflight(args)

    canonical = mac_home / "bin" / "openshell"
    receipt = mac_home / "openshell" / "reviewed-cli.json"
    assert canonical.is_file() and not canonical.is_symlink()
    assert canonical.stat().st_mode & 0o777 == 0o700
    assert canonical.parent.stat().st_mode & 0o777 == 0o700
    assert receipt.stat().st_mode & 0o777 == 0o600
    assert first["status"] == second["status"] == "ready"
    assert first["cli_sha256"] == second["cli_sha256"]
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["cli_sha256"] == second["cli_sha256"]
    assert payload["asset_sha256"] == "a" * 64


def test_group_writable_canonical_directory_is_never_trusted(tmp_path: Path) -> None:
    module = _module()
    mac_home = _managed_legacy_home(tmp_path)
    source = tmp_path / "openshell"
    source.write_bytes(b"reviewed")
    source.chmod(0o700)
    args = _args(mac_home, hashlib.sha256(source.read_bytes()).hexdigest())
    module.atomic_publish(args, source)
    (mac_home / "bin").chmod(0o775)

    result = module.preflight(args)

    assert result["status"] == "migration_required"
    assert result["reason"] == "canonical_directory_untrusted"


def test_non_private_receipt_directory_is_never_trusted(tmp_path: Path) -> None:
    module = _module()
    mac_home = _managed_legacy_home(tmp_path)
    source = tmp_path / "openshell"
    source.write_bytes(b"reviewed")
    source.chmod(0o700)
    args = _args(mac_home, hashlib.sha256(source.read_bytes()).hexdigest())
    module.atomic_publish(args, source)
    (mac_home / "openshell").chmod(0o775)

    result = module.preflight(args)

    assert result["status"] == "migration_required"
    assert result["reason"] == "reviewed_cli_receipt_directory_untrusted"


def test_preflight_classifies_non_reviewed_cli_bytes_for_migration(tmp_path: Path) -> None:
    module = _module()
    mac_home = _managed_legacy_home(tmp_path)
    source = tmp_path / "openshell"
    source.write_bytes(b"reviewed")
    source.chmod(0o700)
    args = _args(mac_home, hashlib.sha256(source.read_bytes()).hexdigest())
    module.atomic_publish(args, source)
    (mac_home / "bin" / "openshell").write_bytes(b"target-selected")
    (mac_home / "bin" / "openshell").chmod(0o700)

    result = module.preflight(args)

    assert result["status"] == "migration_required"
    assert result["reason"] == "canonical_cli_digest_mismatch"


def test_registry_carries_exact_archive_and_extracted_cli_identities() -> None:
    result = subprocess.run(
        ["bash", "-c", '. "$1"; reviewed_openshell_cli_specs', "bash", str(ASSETS)],
        text=True,
        capture_output=True,
        check=True,
    )
    specs = [line.split(":") for line in result.stdout.splitlines()]

    assert len(specs) == 3
    assert all(len(spec) == 5 for spec in specs)
    assert {(spec[0], spec[1]) for spec in specs} == {
        ("darwin", "aarch64"),
        ("linux", "x86_64"),
        ("linux", "aarch64"),
    }
    assert all(re.fullmatch(r"[0-9a-f]{64}", spec[3]) for spec in specs)
    assert all(re.fullmatch(r"[0-9a-f]{64}", spec[4]) for spec in specs)


def test_helper_installs_only_exact_reviewed_archive_and_rechecks(tmp_path: Path) -> None:
    mac_home = _managed_legacy_home(tmp_path)
    archive, digest, cli_digest = _reviewed_archive(tmp_path)
    os_kind, arch, asset, _, _ = _host_values()
    common = [
        "--mac-home",
        str(mac_home),
        "--expected-os",
        os_kind,
        "--version",
        "0.0.72",
        "--base-url",
        "https://github.com/NVIDIA/OpenShell/releases/download/v0.0.72",
        "--asset-spec",
        f"{os_kind}:{arch}:{asset}:{digest}:{cli_digest}",
    ]
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "install-archive",
            *common,
            "--archive",
            str(archive),
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    assert payload["status"] == "ready"
    assert payload["reason"] == "reviewed_cli_ready"


def test_helper_rejects_archive_outside_reviewed_digest(tmp_path: Path) -> None:
    module = _module()
    mac_home = _managed_legacy_home(tmp_path)
    archive, _digest, _cli_digest = _reviewed_archive(tmp_path)
    args = _args(mac_home)

    with pytest.raises(ValueError, match="asset digest mismatch"):
        module.extract_reviewed_archive(args, archive)

    assert not (mac_home / "bin" / "openshell").exists()


def test_helper_rejects_arbitrary_source_publish_action(tmp_path: Path) -> None:
    mac_home = _managed_legacy_home(tmp_path)
    source = tmp_path / "openshell"
    source.write_bytes(b"unreviewed-cli")
    source.chmod(0o700)
    os_kind, arch, asset, digest, cli_digest = _host_values()
    result = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "publish",
            "--mac-home",
            str(mac_home),
            "--expected-os",
            os_kind,
            "--version",
            "0.0.72",
            "--base-url",
            "https://github.com/NVIDIA/OpenShell/releases/download/v0.0.72",
            "--asset-spec",
            f"{os_kind}:{arch}:{asset}:{digest}:{cli_digest}",
            "--source",
            str(source),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "invalid choice" in result.stderr


def test_controller_orders_read_only_preflight_before_migration_and_phase1_wal() -> None:
    text = CONTROLLER.read_text(encoding="utf-8")
    function = text.split("prepare_reviewed_openshell_cli_prerequisites() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert function.index("classifying every reviewed OpenShell CLI prerequisite") < function.index(
        "applying exact reviewed OpenShell CLI prerequisite migrations"
    )
    cohort = text.split("run_typed_cohort() {", 1)[1]
    phase1_worker = text.split("typed_phase1_prepare_worker() {", 1)[1].split(
        "\n}\n\nstart_control_master_worker", 1
    )[0]
    assert cohort.index("classify_reviewed_openshell_cli_prerequisites") < cohort.index(
        "cohort_journal_mutate phase1-prepare-start"
    )
    assert "prepare_remote_phase1_restore_contract" in phase1_worker
    assert cohort.index("classify_reviewed_openshell_cli_prerequisites") < cohort.index(
        'run_bounded_node_phase "$selected_specs_file" phase1-prepare'
    )
    assert "prepare_reviewed_openshell_cli_prerequisites" not in cohort.split(
        'echo "==> fleet: arming exact phase-1 restore contracts"', 1
    )[0]
    legacy = text.split("legacy_hub_bootstrap() {", 1)[1].split(
        "\n}\n\nhub_epoch_client_read", 1
    )[0]
    assert legacy.index("classify_reviewed_openshell_cli_prerequisites") < legacy.index(
        "preflight_legacy_hub_prerequisites"
    )
    assert legacy.index("classify_reviewed_openshell_cli_prerequisites") < legacy.index(
        "prepare_remote_phase1_restore_contract"
    )


def test_bootstrap_uses_exact_archive_installer() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert 'reviewed-cli-assets.sh' in text
    assert 'reviewed-cli.py' in text
    assert 'ln -sf "$cli" "$MAC_HOME/bin/openshell"' not in text
    assert 'python3 "$helper" install-archive' in text
    assert '--archive "$archive"' in text
    assert 'install -m755 "$MAC_HOME/bin/openshell" "$BIN/openshell"' in text
    assert 'python3 "$helper" publish' not in text


def test_phase1_binds_exact_reviewed_cli_and_receipt_digests() -> None:
    text = INSTALLER.read_text(encoding="utf-8")
    assert "MAC_DEPLOY_REVIEWED_OPENSHELL_CLI_SHA256" in text
    assert "MAC_DEPLOY_REVIEWED_OPENSHELL_RECEIPT_SHA256" in text
    assert "reviewed OpenShell CLI digest changed after preflight" in text
    assert "reviewed OpenShell CLI receipt changed after preflight" in text
    controller = CONTROLLER.read_text(encoding="utf-8")
    assert "MAC_PHASE1_OSH_CLI_SHA" in controller
    assert "MAC_PHASE1_OSH_RECEIPT_SHA" in controller


def _controller_status_validator() -> str:
    controller = CONTROLLER.read_text(encoding="utf-8")
    marker = '"${identity_specs[@]}" <<\'PY\'\n'
    return controller.split(marker, 1)[1].split("\nPY\n", 1)[0]


def _run_controller_status_validator(
    tmp_path: Path,
    status: dict[str, object],
    *,
    required: str = "true",
) -> subprocess.CompletedProcess[str]:
    status_path = tmp_path / "status.json"
    status_path.write_text(json.dumps(status), encoding="utf-8")
    identity = ":".join(
        [
            "linux",
            "x86_64",
            "openshell-x86_64-unknown-linux-musl.tar.gz",
            "a" * 64,
            "b" * 64,
        ]
    )
    return subprocess.run(
        [
            sys.executable,
            "-",
            str(status_path),
            "preflight",
            "linux",
            required,
            "0.0.72",
            identity,
        ],
        input=_controller_status_validator(),
        text=True,
        capture_output=True,
        check=False,
    )


def _ready_controller_status() -> dict[str, object]:
    return {
        "schema": "mac.reviewed_openshell_cli_preflight.v1",
        "expected_os": "linux",
        "arch": "x86_64",
        "version": "0.0.72",
        "asset": "openshell-x86_64-unknown-linux-musl.tar.gz",
        "asset_sha256": "a" * 64,
        "managed_openclaw": True,
        "required": True,
        "status": "ready",
        "reason": "reviewed_cli_ready",
        "cli_sha256": "b" * 64,
        "receipt_sha256": "c" * 64,
    }


@pytest.mark.parametrize(
    ("key", "forged"),
    [
        ("version", "0.0.71"),
        ("asset", "openshell-forged-linux-musl.tar.gz"),
        ("asset_sha256", "d" * 64),
        ("cli_sha256", "e" * 64),
    ],
)
def test_controller_rejects_shape_valid_target_selected_cli_identity(
    tmp_path: Path, key: str, forged: str
) -> None:
    status = _ready_controller_status()
    status[key] = forged

    result = _run_controller_status_validator(tmp_path, status)

    assert result.returncode != 0
    assert "reviewed OpenShell CLI" in result.stderr


def test_controller_accepts_only_exact_reviewed_cli_identity(tmp_path: Path) -> None:
    result = _run_controller_status_validator(tmp_path, _ready_controller_status())

    assert result.returncode == 0, result.stderr


def test_controller_rejects_required_policy_downgrade(tmp_path: Path) -> None:
    status = _ready_controller_status()
    status.update(
        managed_openclaw=False,
        required=False,
        reason="openclaw_not_managed",
    )
    result = _run_controller_status_validator(tmp_path, status, required="true")

    assert result.returncode != 0
    assert "weakened the fleet requirement" in result.stderr


def _shell_function(text: str, name: str) -> str:
    body = text.split(f"{name}() {{", 1)[1].split("\n}\n", 1)[0]
    return f"{name}() {{{body}\n}}\n"


def _prepare_spec(agent: str) -> str:
    fields = [""] * 54
    fields[0] = agent
    fields[2] = "linux"
    fields[53] = "1"
    return "|".join(fields)


def _run_prepare_harness(
    tmp_path: Path, reasons: dict[str, str]
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    selected = tmp_path / "selected"
    selected.write_text(
        "\n".join(_prepare_spec(agent) for agent in reasons) + "\n", encoding="utf-8"
    )
    calls = tmp_path / "migrate-calls"
    controller = CONTROLLER.read_text(encoding="utf-8")
    reason_cases = "\n".join(
        f"    {agent}) printf '%s\\n' '{reason}' ;;" for agent, reason in reasons.items()
    )
    harness = f"""
set -euo pipefail
{_shell_function(controller, "reviewed_openshell_cli_migration_repairable_reason")}
{_shell_function(controller, "prepare_reviewed_openshell_cli_prerequisites")}
reviewed_openshell_cli_status_file() {{ printf '/tmp/%s.json\\n' "$1"; }}
run_remote_reviewed_openshell_cli_helper() {{
  if [ "$1" = migrate ]; then printf '%s\\n' "$2" >> {calls}; fi
}}
reviewed_openshell_cli_status_value() {{
  agent="$1"; key="$2"
  if [ "$key" = status ]; then
    if [ -f {calls} ] && grep -Fqx "$agent" {calls}; then printf 'ready\\n'; else printf 'migration_required\\n'; fi
    return
  fi
  if [ -f {calls} ] && grep -Fqx "$agent" {calls}; then printf 'reviewed_cli_ready\\n'; return; fi
  case "$agent" in
{reason_cases}
  esac
}}
prepare_reviewed_openshell_cli_prerequisites {selected}
"""
    result = subprocess.run(
        ["bash", "-c", harness], text=True, capture_output=True, check=False
    )
    migrated = calls.read_text(encoding="utf-8").splitlines() if calls.exists() else []
    return result, migrated


@pytest.mark.parametrize(
    "blocked_reason", ["managed_openclaw_identity_untrusted", "future_unknown_reason"]
)
def test_prepare_rejects_unrepairable_cohort_before_any_mutation(
    tmp_path: Path, blocked_reason: str
) -> None:
    result, migrated = _run_prepare_harness(
        tmp_path,
        {
            "repairable-first": "canonical_cli_missing",
            "blocked-second": blocked_reason,
        },
    )

    assert result.returncode != 0
    assert migrated == []
    assert "refusing all reviewed OpenShell CLI mutations" in result.stderr


def test_prepare_migrates_only_allowlisted_repairable_cohort(tmp_path: Path) -> None:
    result, migrated = _run_prepare_harness(
        tmp_path,
        {
            "repairable-cli": "canonical_cli_missing",
            "repairable-receipt": "reviewed_cli_receipt_mismatch",
        },
    )

    assert result.returncode == 0, result.stderr
    assert migrated == ["repairable-cli", "repairable-receipt"]


def test_patch_does_not_publish_or_strengthen_runtime_attestation() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    controller = CONTROLLER.read_text(encoding="utf-8")
    assert "require-runtime-attestation" not in helper
    assert "mac.openshell_runtime_attestation.v1" not in installer
    builder = controller.split("prepare_remote_prerequisite_bundle() {", 1)[1].split(
        "\n}\n", 1
    )[0]
    assert "MAC_PREREQ_OPENSHELL_RUNTIME_ATTESTATION_SHA" not in builder
    assert "stable_private_path_check" not in builder
    assert 'openshell_path = mac_home / "openshell" / "runtime-image-attestation.json"' in builder
    assert '"openshell": [path_check("openshell-contract", openshell_path)]' in builder
