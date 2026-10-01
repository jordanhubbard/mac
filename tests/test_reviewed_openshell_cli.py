from __future__ import annotations

import argparse
import hashlib
import importlib.util
import io
import json
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
BOOTSTRAP = ROOT / "deploy" / "openshell" / "bootstrap-openshell.sh"


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
        asset_spec=[f"{os_kind}:{arch}:{asset}:{digest}:{cli_digest or default_cli_digest}"],
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


def test_not_required_but_already_installed_cli_still_gets_full_identity(
    tmp_path: Path,
) -> None:
    """OpenClaw need not be sandbox-managed for a node to have OpenShell
    installed (e.g. mac-agent's own task-sandbox use, independent of
    OpenClaw). Quiescence's stray-sandbox inventory check
    (list_openshell_sandboxes) requires a full, trustworthy reviewed
    identity whenever OpenShell is installed at all, regardless of whether
    OpenClaw itself is managed -- so preflight must not take the
    "openclaw_not_managed" short-circuit (which omits cli_sha256 and
    receipt_sha256) once a canonical CLI is actually present on disk."""
    module = _module()
    mac_home = tmp_path / ".mac"
    source = tmp_path / "openshell"
    source.write_bytes(b"#!/bin/sh\nexit 0\n")
    source.chmod(0o700)
    args = _args(mac_home, hashlib.sha256(source.read_bytes()).hexdigest())
    module.atomic_publish(args, source)

    result = module.preflight(args)

    assert result["managed_openclaw"] is False
    assert result["required"] is True
    assert result["status"] == "ready"
    assert result["reason"] == "reviewed_cli_ready"
    assert "cli_sha256" in result and result["cli_sha256"]
    assert "receipt_sha256" in result and result["receipt_sha256"]


def test_not_required_and_never_installed_still_short_circuits(tmp_path: Path) -> None:
    """When OpenClaw isn't managed AND no CLI was ever installed, there is
    nothing to validate -- the short-circuit "ready"/"openclaw_not_managed"
    result (no cli_sha256/receipt_sha256) is correct and expected."""
    module = _module()
    mac_home = tmp_path / ".mac"
    args = _args(mac_home)

    result = module.preflight(args)

    assert result["managed_openclaw"] is False
    assert result["required"] is False
    assert result["status"] == "ready"
    assert result["reason"] == "openclaw_not_managed"
    assert "cli_sha256" not in result
    assert "receipt_sha256" not in result


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

    gateway_result = subprocess.run(
        ["bash", "-c", '. "$1"; reviewed_openshell_gateway_specs', "bash", str(ASSETS)],
        text=True,
        capture_output=True,
        check=True,
    )
    gateway_specs = [line.split(":") for line in gateway_result.stdout.splitlines()]
    assert len(gateway_specs) == 2
    assert all(len(spec) == 5 for spec in gateway_specs)
    assert {(spec[0], spec[1]) for spec in gateway_specs} == {
        ("linux", "x86_64"),
        ("linux", "aarch64"),
    }
    assert all(spec[2].startswith("openshell-gateway-") for spec in gateway_specs)
    assert all(re.fullmatch(r"[0-9a-f]{64}", spec[3]) for spec in gateway_specs)
    assert all(re.fullmatch(r"[0-9a-f]{64}", spec[4]) for spec in gateway_specs)


def test_linux_preflight_rejects_schema_incompatible_gateway_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    mac_home = _managed_legacy_home(tmp_path)
    cli = tmp_path / "openshell"
    cli.write_bytes(b"reviewed-cli")
    cli.chmod(0o700)
    gateway = tmp_path / "openshell-gateway"
    gateway.write_bytes(b"reviewed-gateway")
    gateway.chmod(0o700)
    args = argparse.Namespace(
        action="preflight",
        mac_home=str(mac_home),
        expected_os="linux",
        version="0.0.72",
        base_url="https://github.com/NVIDIA/OpenShell/releases/download/v0.0.72",
        asset_spec=[
            "linux:x86_64:openshell-x86_64-test.tar.gz:"
            + "a" * 64
            + ":"
            + hashlib.sha256(cli.read_bytes()).hexdigest()
        ],
        gateway_asset_spec=[
            "linux:x86_64:openshell-gateway-x86_64-test.tar.gz:"
            + "b" * 64
            + ":"
            + hashlib.sha256(gateway.read_bytes()).hexdigest()
        ],
        archive=None,
        required=True,
    )
    module.atomic_publish(args, cli)
    installed_gateway = tmp_path / ".local" / "bin" / "openshell-gateway"
    installed_gateway.parent.mkdir(parents=True, mode=0o700)
    installed_gateway.write_bytes(b"legacy-gateway")
    installed_gateway.chmod(0o700)

    mismatch = module.preflight(args)
    assert mismatch["status"] == "migration_required"
    assert mismatch["reason"] == "canonical_gateway_digest_mismatch"

    module.atomic_publish_gateway(args, gateway)
    ready = module.preflight(args)
    assert ready["status"] == "ready"
    assert ready["gateway_sha256"] == hashlib.sha256(gateway.read_bytes()).hexdigest()


def test_linux_untrusted_managed_identity_retains_reviewed_gateway_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _module()
    monkeypatch.setattr(module.platform, "system", lambda: "Linux")
    monkeypatch.setattr(module.platform, "machine", lambda: "x86_64")
    mac_home = tmp_path / ".mac"
    managed = mac_home / "openclaw" / "managed"
    managed.mkdir(parents=True, mode=0o700)
    runtime = managed / "runtime.env"
    runtime.write_text("MAC_OPENCLAW_SANDBOX=\n", encoding="utf-8")
    runtime.chmod(0o600)
    args = argparse.Namespace(
        action="preflight",
        mac_home=str(mac_home),
        expected_os="linux",
        version="0.0.72",
        base_url="https://github.com/NVIDIA/OpenShell/releases/download/v0.0.72",
        asset_spec=["linux:x86_64:openshell-x86_64-test.tar.gz:" + "a" * 64 + ":" + "b" * 64],
        gateway_asset_spec=[
            "linux:x86_64:openshell-gateway-x86_64-test.tar.gz:" + "c" * 64 + ":" + "d" * 64
        ],
        archive=None,
        required=True,
    )

    result = module.preflight(args)

    assert result["status"] == "migration_required"
    assert result["reason"] == "managed_openclaw_identity_untrusted"
    assert result["gateway_asset"] == "openshell-gateway-x86_64-test.tar.gz"
    assert result["gateway_asset_sha256"] == "c" * 64


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


def test_bootstrap_uses_exact_archive_installer() -> None:
    text = BOOTSTRAP.read_text(encoding="utf-8")
    assert "reviewed-cli-assets.sh" in text
    assert "reviewed-cli.py" in text
    assert 'ln -sf "$cli" "$MAC_HOME/bin/openshell"' not in text
    assert 'python3 "$helper" install-archive' in text
    assert '--archive "$archive"' in text
    assert 'install -m755 "$MAC_HOME/bin/openshell" "$BIN/openshell"' in text
    assert 'python3 "$helper" publish' not in text


def test_patch_does_not_publish_or_strengthen_runtime_attestation() -> None:
    helper = HELPER.read_text(encoding="utf-8")
    assert "require-runtime-attestation" not in helper
