#!/usr/bin/env python3
"""Preflight and publish the exact reviewed OpenShell CLI used by phase 1."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path


SCHEMA = "mac.reviewed_openshell_cli.v1"
PREFLIGHT_SCHEMA = "mac.reviewed_openshell_cli_preflight.v1"
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_BINARY_BYTES = 128 * 1024 * 1024


def normalized_os(value: str) -> str:
    result = value.strip().lower()
    if result not in {"darwin", "linux"}:
        raise ValueError("unsupported reviewed OpenShell operating system")
    return result


def normalized_arch(value: str) -> str:
    result = value.strip().lower()
    aliases = {"amd64": "x86_64", "arm64": "aarch64"}
    result = aliases.get(result, result)
    if result not in {"x86_64", "aarch64"}:
        raise ValueError("unsupported reviewed OpenShell architecture")
    return result


def parse_specs(values: list[str]) -> dict[tuple[str, str], tuple[str, str, str]]:
    specs: dict[tuple[str, str], tuple[str, str, str]] = {}
    for raw in values:
        parts = raw.split(":", 4)
        if len(parts) != 5:
            raise ValueError("reviewed OpenShell asset spec is malformed")
        os_kind, arch, asset, digest, cli_digest = parts
        key = (normalized_os(os_kind), normalized_arch(arch))
        if key in specs or not re.fullmatch(r"openshell-[A-Za-z0-9._-]+\.tar\.gz", asset):
            raise ValueError("reviewed OpenShell asset spec is ambiguous")
        if (
            re.fullmatch(r"[0-9a-f]{64}", digest) is None
            or re.fullmatch(r"[0-9a-f]{64}", cli_digest) is None
        ):
            raise ValueError("reviewed OpenShell asset digest is malformed")
        specs[key] = (asset, digest, cli_digest)
    if not specs:
        raise ValueError("reviewed OpenShell asset specs are required")
    return specs


def stable_bytes(path: Path, *, private: bool, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_uid != os.getuid():
            raise ValueError("artifact is not an owner-controlled regular file")
        mode = stat.S_IMODE(before.st_mode)
        if (private and mode != 0o600) or (not private and mode & 0o022):
            raise ValueError("artifact permissions are not trusted")
        if before.st_nlink != 1 or before.st_size <= 0 or before.st_size > limit:
            raise ValueError("artifact size or link count is unsafe")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("artifact changed while reading")
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ValueError("artifact changed while reading")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def managed_openclaw(mac_home: Path) -> bool:
    managed = mac_home / "openclaw" / "managed"
    sandbox = managed / "sandbox-name"
    runtime = managed / "runtime.env"
    if sandbox.exists() or sandbox.is_symlink():
        raw = stable_bytes(sandbox, private=True, limit=1024 * 1024)
        text = raw.decode("utf-8", errors="strict")
        if "\n" in text.rstrip("\n") or not text.strip():
            raise ValueError("managed OpenClaw sandbox identity is malformed")
        return True
    if runtime.exists() or runtime.is_symlink():
        raw = stable_bytes(runtime, private=True, limit=1024 * 1024)
        text = raw.decode("utf-8", errors="strict")
        matches = re.findall(
            r"(?m)^[ \t]*(?:export[ \t]+)?MAC_OPENCLAW_SANDBOX[ \t]*=(.*)$",
            text,
        )
        if len(matches) != 1 or not matches[0].strip():
            raise ValueError("managed OpenClaw runtime lacks one sandbox identity")
        return True
    try:
        if managed.is_dir() and any(managed.iterdir()):
            raise ValueError("managed OpenClaw artifacts lack a sandbox identity")
    except OSError as exc:
        raise ValueError("managed OpenClaw artifact directory is unreadable") from exc
    return False


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def preflight(args: argparse.Namespace) -> dict[str, object]:
    mac_home = Path(args.mac_home).expanduser().resolve()
    os_kind = normalized_os(args.expected_os)
    actual_os = normalized_os(platform.system())
    arch = normalized_arch(platform.machine())
    specs = parse_specs(args.asset_spec)
    if actual_os != os_kind:
        raise ValueError("fleet OS declaration differs from the target host")
    try:
        asset, asset_sha, expected_cli_sha = specs[(os_kind, arch)]
    except KeyError as exc:
        raise ValueError("target has no reviewed OpenShell CLI asset") from exc
    try:
        managed = managed_openclaw(mac_home)
    except (OSError, UnicodeError, ValueError):
        return {
            "schema": PREFLIGHT_SCHEMA,
            "expected_os": os_kind,
            "arch": arch,
            "version": args.version,
            "asset": asset,
            "asset_sha256": asset_sha,
            "managed_openclaw": True,
            "required": True,
            "status": "migration_required",
            "reason": "managed_openclaw_identity_untrusted",
        }
    required = bool(args.required) or managed
    base: dict[str, object] = {
        "schema": PREFLIGHT_SCHEMA,
        "expected_os": os_kind,
        "arch": arch,
        "version": args.version,
        "asset": asset,
        "asset_sha256": asset_sha,
        "managed_openclaw": managed,
        "required": required,
    }
    if not required:
        return {**base, "status": "ready", "reason": "openclaw_not_managed"}

    canonical_dir = mac_home / "bin"
    canonical = canonical_dir / "openshell"
    receipt_path = mac_home / "openshell" / "reviewed-cli.json"
    try:
        parent = canonical_dir.lstat()
    except OSError:
        return {**base, "status": "migration_required", "reason": "canonical_directory_missing"}
    if (
        not stat.S_ISDIR(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) != 0o700
    ):
        return {**base, "status": "migration_required", "reason": "canonical_directory_untrusted"}
    receipt_dir = receipt_path.parent
    try:
        receipt_parent = receipt_dir.lstat()
    except OSError:
        return {
            **base,
            "status": "migration_required",
            "reason": "reviewed_cli_receipt_directory_untrusted",
        }
    if (
        not stat.S_ISDIR(receipt_parent.st_mode)
        or receipt_parent.st_uid != os.getuid()
        or stat.S_IMODE(receipt_parent.st_mode) != 0o700
    ):
        return {
            **base,
            "status": "migration_required",
            "reason": "reviewed_cli_receipt_directory_untrusted",
        }
    try:
        binary_raw = stable_bytes(canonical, private=False, limit=MAX_BINARY_BYTES)
    except FileNotFoundError:
        return {**base, "status": "migration_required", "reason": "canonical_cli_missing"}
    except (OSError, ValueError):
        return {**base, "status": "migration_required", "reason": "canonical_cli_untrusted"}
    if not os.access(canonical, os.X_OK):
        return {**base, "status": "migration_required", "reason": "canonical_cli_not_executable"}
    cli_sha = sha256(binary_raw)
    if cli_sha != expected_cli_sha:
        return {**base, "status": "migration_required", "reason": "canonical_cli_digest_mismatch"}
    try:
        receipt_raw = stable_bytes(receipt_path, private=True, limit=1024 * 1024)
        receipt = json.loads(receipt_raw)
    except FileNotFoundError:
        return {**base, "status": "migration_required", "reason": "reviewed_cli_receipt_missing"}
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return {**base, "status": "migration_required", "reason": "reviewed_cli_receipt_untrusted"}
    expected = {
        "schema": SCHEMA,
        "status": "published",
        "version": args.version,
        "os": os_kind,
        "arch": arch,
        "asset": asset,
        "asset_sha256": asset_sha,
        "cli_path": str(canonical),
        "cli_sha256": cli_sha,
    }
    if not isinstance(receipt, dict) or any(receipt.get(k) != v for k, v in expected.items()):
        return {**base, "status": "migration_required", "reason": "reviewed_cli_receipt_mismatch"}
    if not isinstance(receipt.get("recorded_at"), str):
        return {**base, "status": "migration_required", "reason": "reviewed_cli_receipt_mismatch"}
    result = {
        **base,
        "status": "ready",
        "reason": "reviewed_cli_ready",
        "cli_path": str(canonical),
        "cli_sha256": cli_sha,
        "receipt_path": str(receipt_path),
        "receipt_sha256": sha256(receipt_raw),
    }
    return result


def atomic_publish(args: argparse.Namespace, source: Path) -> None:
    mac_home = Path(args.mac_home).expanduser().resolve()
    os_kind = normalized_os(args.expected_os)
    arch = normalized_arch(platform.machine())
    specs = parse_specs(args.asset_spec)
    asset, asset_sha, expected_cli_sha = specs[(os_kind, arch)]
    source_raw = stable_bytes(source, private=False, limit=MAX_BINARY_BYTES)
    if sha256(source_raw) != expected_cli_sha:
        raise ValueError("reviewed OpenShell CLI digest differs from its reviewed archive")
    canonical_dir = mac_home / "bin"
    receipt_dir = mac_home / "openshell"
    for directory in (mac_home, canonical_dir, receipt_dir):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = directory.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError("reviewed OpenShell directory is not owner-controlled")
        os.chmod(directory, 0o700)
    canonical = canonical_dir / "openshell"
    descriptor, temporary_raw = tempfile.mkstemp(prefix=".openshell.", dir=canonical_dir)
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, 0o700)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(source_raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, canonical)
        os.chmod(canonical, 0o700)
        directory_fd = os.open(canonical_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)
    payload = {
        "schema": SCHEMA,
        "status": "published",
        "version": args.version,
        "os": os_kind,
        "arch": arch,
        "asset": asset,
        "asset_sha256": asset_sha,
        "cli_path": str(canonical),
        "cli_sha256": sha256(source_raw),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    receipt = receipt_dir / "reviewed-cli.json"
    descriptor, temporary_raw = tempfile.mkstemp(prefix=".reviewed-cli.", dir=receipt_dir)
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, receipt)
        directory_fd = os.open(receipt_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def extract_reviewed_archive(args: argparse.Namespace, archive: Path) -> Path:
    os_kind = normalized_os(args.expected_os)
    arch = normalized_arch(platform.machine())
    _asset, expected_sha, _expected_cli_sha = parse_specs(args.asset_spec)[(os_kind, arch)]
    raw = stable_bytes(archive, private=False, limit=MAX_ARCHIVE_BYTES)
    if sha256(raw) != expected_sha:
        raise ValueError("reviewed OpenShell asset digest mismatch")
    temporary_dir = Path(tempfile.mkdtemp(prefix="mac-reviewed-openshell-"))
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as bundle:
            members = bundle.getmembers()
            candidates = [
                item
                for item in members
                if item.isfile() and Path(item.name).name == "openshell"
            ]
            if len(members) > 256 or len(candidates) != 1:
                raise ValueError("reviewed OpenShell archive shape is invalid")
            member = candidates[0]
            if member.size <= 0 or member.size > MAX_BINARY_BYTES:
                raise ValueError("reviewed OpenShell binary size is invalid")
            stream = bundle.extractfile(member)
            if stream is None:
                raise ValueError("reviewed OpenShell binary is unreadable")
            binary = temporary_dir / "openshell"
            descriptor = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
            try:
                remaining = member.size
                while remaining:
                    chunk = stream.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ValueError("reviewed OpenShell archive was truncated")
                    pending = memoryview(chunk)
                    while pending:
                        written = os.write(descriptor, pending)
                        if written <= 0:
                            raise ValueError("reviewed OpenShell binary could not be written")
                        pending = pending[written:]
                    remaining -= len(chunk)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        return binary
    except (OSError, tarfile.TarError):
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise ValueError("reviewed OpenShell archive is unreadable") from None
    except Exception:
        shutil.rmtree(temporary_dir, ignore_errors=True)
        raise


def download_and_extract(args: argparse.Namespace) -> Path:
    os_kind = normalized_os(args.expected_os)
    arch = normalized_arch(platform.machine())
    asset, _expected_sha, _expected_cli_sha = parse_specs(args.asset_spec)[(os_kind, arch)]
    if not re.fullmatch(r"https://github\.com/NVIDIA/OpenShell/releases/download/v[0-9.]+", args.base_url):
        raise ValueError("reviewed OpenShell base URL is not allowed")
    temporary_dir = Path(tempfile.mkdtemp(prefix="mac-reviewed-openshell-"))
    archive = temporary_dir / asset
    curl = shutil.which("curl")
    if not curl:
        raise ValueError("curl is required for reviewed OpenShell migration")
    result = subprocess.run(
        [
            curl,
            "--fail",
            "--silent",
            "--show-error",
            "--location",
            "--proto",
            "=https",
            "--tlsv1.2",
            "--retry",
            "3",
            "--retry-all-errors",
            "--connect-timeout",
            "15",
            "--max-time",
            "120",
            "--max-filesize",
            str(MAX_ARCHIVE_BYTES),
            "--output",
            str(archive),
            args.base_url.rstrip("/") + "/" + asset,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=130,
        check=False,
    )
    if result.returncode != 0:
        shutil.rmtree(temporary_dir)
        raise ValueError("reviewed OpenShell asset download failed")
    os.chmod(archive, 0o600)
    try:
        return extract_reviewed_archive(args, archive)
    finally:
        shutil.rmtree(temporary_dir, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices={"preflight", "migrate", "install-archive"})
    parser.add_argument("--mac-home", required=True)
    parser.add_argument("--expected-os", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--asset-spec", action="append", default=[])
    parser.add_argument("--archive")
    parser.add_argument("--required", action="store_true")
    args = parser.parse_args()
    if args.action == "preflight":
        print(json.dumps(preflight(args), sort_keys=True, separators=(",", ":")))
        return 0
    if args.action == "install-archive":
        if not args.archive:
            raise SystemExit("--archive is required for install-archive")
        binary = extract_reviewed_archive(args, Path(args.archive))
        try:
            atomic_publish(args, binary)
        finally:
            shutil.rmtree(binary.parent, ignore_errors=True)
    else:
        before = preflight(args)
        if before.get("status") != "ready":
            binary = download_and_extract(args)
            try:
                atomic_publish(args, binary)
            finally:
                shutil.rmtree(binary.parent, ignore_errors=True)
    result = preflight(args)
    if result.get("status") != "ready":
        raise SystemExit("reviewed OpenShell CLI migration did not converge")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
