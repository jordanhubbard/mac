#!/usr/bin/env python3
"""Prepare and publish the rollback baseline for a pristine fungible node.

This helper is intentionally a pre-cohort operation.  It does not install,
stop, start, or inspect the contents of a MAC service.  It only accepts a node
that has never reached phase 2 (or a failed attempt that still has neither
canonical source nor venv), prepares exact reviewed assets in a
generation-scoped directory, and publishes a complete source/venv/tool
baseline after the controller has registered a draining fungible placeholder.

The remote system Python only needs to run this standard-library helper.  The
managed runtime is pinned by deploy/reviewed-tool-assets.sh:

* uv 0.8.22
* CPython 3.12.11 installed by that reviewed uv
* CodeGraph v1.1.6

Publication is receipt-atomic: every canonical path is created while an
exclusive owner lock is held, exact readback is performed, and an owner-only,
fsynced receipt is the commit marker.  Any failure before that marker removes
only artifacts created by this generation and restores the pristine baseline.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Sequence


STAGE_SCHEMA = "mac.fleet_machine_onboarding_stage.v1"
PLACEHOLDER_SCHEMA = "mac.fleet_machine_onboarding_placeholder.v1"
RECEIPT_SCHEMA = "mac.fleet_machine_onboarding_receipt.v1"
STATUS_SCHEMA = "mac.fleet_machine_onboarding_status.v1"
ROUTE_SCHEMA = "mac.fleet_endpoint_identity.v1"
UV_VERSION = "0.8.22"
PYTHON_VERSION = "3.12.11"
CODEGRAPH_VERSION = "v1.1.6"
MAX_JSON_BYTES = 1024 * 1024
SAFE_GENERATION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}$")
HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class OnboardingError(RuntimeError):
    """A node cannot safely enter or complete machine onboarding."""


@dataclass(frozen=True)
class Layout:
    home: Path
    mac_home: Path
    source: Path
    venv: Path
    local_bin: Path
    mac_bin: Path
    codegraph_bin: Path
    gh_bin: Path
    receipt: Path
    lock: Path

    @classmethod
    def for_home(cls, home: Path, mac_home: Path | None = None) -> "Layout":
        home = home.expanduser().absolute()
        root = (mac_home or home / ".mac").expanduser().absolute()
        return cls(
            home=home,
            mac_home=root,
            source=root / "src" / "mac",
            venv=root / "venv",
            local_bin=home / ".local" / "bin",
            mac_bin=home / ".local" / "bin" / "mac",
            codegraph_bin=root / "bin" / "codegraph",
            gh_bin=root / "bin" / "gh",
            receipt=root / "machine-onboarding-receipt.json",
            lock=root / ".machine-onboarding.lock",
        )

    def stage(self, generation: str) -> Path:
        return self.mac_home / "onboarding" / "generations" / generation


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OnboardingError(f"not a regular file: {path}")
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
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
            raise OnboardingError(f"file changed while hashing: {path}")
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_private_json(path: Path, payload: dict[str, Any]) -> None:
    body = _canonical(payload)
    if not 1 <= len(body) <= MAX_JSON_BYTES:
        raise OnboardingError("onboarding JSON exceeds its safe bound")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _private_json(path: Path, expected_schema: str) -> dict[str, Any]:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1
            or not 1 <= before.st_size <= MAX_JSON_BYTES
        ):
            raise OnboardingError(f"private JSON is unsafe: {path}")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (
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
            raise OnboardingError(f"private JSON changed while reading: {path}")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise OnboardingError(f"private JSON is malformed: {path}") from exc
    if not isinstance(value, dict) or value.get("schema") != expected_schema:
        raise OnboardingError(f"private JSON schema is invalid: {path}")
    return value


def _safe_generation(value: str) -> str:
    if SAFE_GENERATION.fullmatch(value or "") is None:
        raise OnboardingError("onboarding generation is invalid")
    return value


def _ensure_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise OnboardingError(f"directory is not owner-controlled: {path}")
    os.chmod(path, 0o700)


@contextlib.contextmanager
def onboarding_lock(layout: Layout) -> Iterator[None]:
    _ensure_private_directory(layout.mac_home)
    descriptor = os.open(
        layout.lock,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _path_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _mac_env_has_generation(path: Path) -> bool:
    if not _path_exists(path):
        return False
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise OnboardingError("existing mac.env is not a regular file")
    if metadata.st_size > MAX_JSON_BYTES:
        raise OnboardingError("existing mac.env exceeds its safe bound")
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if stripped.startswith("MAC_WORKER_DEPLOY_GENERATION="):
            return True
    return False


def _service_configuration_paths(layout: Layout) -> list[Path]:
    patterns = (
        layout.home / ".config" / "systemd" / "user" / "mac*.service",
        layout.home / "Library" / "LaunchAgents" / "com.mac.*.plist",
        layout.mac_home / "supervisord*.conf",
        layout.mac_home / "services" / "*",
    )
    found: list[Path] = []
    for pattern in patterns:
        found.extend(pattern.parent.glob(pattern.name))
    for root, pattern in (
        (Path("/etc/systemd/system"), "mac*.service"),
        (Path("/etc/supervisor/conf.d"), "mac*.conf"),
        (Path("/Library/LaunchDaemons"), "com.mac.*.plist"),
    ):
        try:
            found.extend(root.glob(pattern))
        except OSError:
            continue
    return sorted(
        {
            item
            for item in found
            if _path_exists(item)
            # Phase-zero runs after the separately authorized network
            # prerequisite operation. Its fleet-scoped tailscaled program is
            # route infrastructure, not a MAC application generation, and
            # must remain live so the typed deploy can reach the node.
            and not (
                item.parent == Path("/etc/supervisor/conf.d")
                and item.name.endswith("-tailscaled.conf")
            )
        }
    )


def _service_processes(supervisor: str) -> list[str]:
    commands: list[Sequence[str]] = []
    if supervisor in {"auto", "systemd"} and shutil.which("systemctl"):
        commands.extend(
            (
                ("systemctl", "--user", "--no-pager", "--plain", "list-units", "--all"),
                ("systemctl", "--no-pager", "--plain", "list-units", "--all"),
            )
        )
    if supervisor in {"auto", "supervisord"} and shutil.which("supervisorctl"):
        commands.append(("supervisorctl", "status"))
    if supervisor in {"auto", "launchd"} and shutil.which("launchctl"):
        commands.append(("launchctl", "list"))
    observed: list[str] = []
    service_name = re.compile(
        r"(?i)(?:^|[\s./_-])(?:com[.]mac[.]|mac(?:-agent|-hub|-gateway|-router|-worker|_))"
    )
    for command in commands:
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
                env={
                    "HOME": str(Path.home()),
                    "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                },
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in result.stdout.splitlines():
            if service_name.search(line):
                observed.append(line.split(maxsplit=1)[0][:256])
    return sorted(set(observed))


def validate_pristine(layout: Layout, supervisor: str) -> dict[str, Any]:
    """Prove that no deployable generation or MAC service exists."""

    blockers: list[str] = []
    for label, path in (
        ("source", layout.source),
        ("venv", layout.venv),
        ("deployed_revision", layout.mac_home / "deployed-source-revision"),
    ):
        if _path_exists(path):
            blockers.append(label)
    if _mac_env_has_generation(layout.mac_home / "mac.env"):
        blockers.append("worker_generation")
    if _path_exists(layout.receipt):
        blockers.append("committed_onboarding_receipt")
    service_paths = _service_configuration_paths(layout)
    service_processes = _service_processes(supervisor)
    if service_paths:
        blockers.append("service_configuration")
    if service_processes:
        blockers.append("service_process")
    if blockers:
        raise OnboardingError(
            "node is not pristine/failed-prephase: " + ",".join(sorted(set(blockers)))
        )
    return {
        "source_absent": True,
        "venv_absent": True,
        "deployed_revision_absent": True,
        "worker_generation_absent": True,
        "service_configuration_absent": True,
        "service_process_absent": True,
    }


def validate_route_identity(path: Path) -> tuple[dict[str, Any], str]:
    value = _private_json(path, ROUTE_SCHEMA)
    if value.get("adapter") != "ssh-machine":
        raise OnboardingError(
            "machine onboarding requires an SSH-machine route identity"
        )
    authority = value.get("authority")
    if not isinstance(authority, dict) or set(authority) != {
        "ssh_host_key_sha256",
        "instance_id_kind",
        "instance_id_sha256",
    }:
        raise OnboardingError("route identity authority is incomplete")
    for key in ("ssh_host_key_sha256", "instance_id_sha256"):
        if HEX_SHA256.fullmatch(str(authority.get(key) or "")) is None:
            raise OnboardingError(f"route identity {key} is invalid")
    return value, hashlib.sha256(_canonical(value)).hexdigest()


def _extract_source_archive(archive: Path, destination: Path) -> None:
    if not archive.is_file() or archive.is_symlink():
        raise OnboardingError("source archive is not a regular file")
    destination.mkdir(mode=0o700)
    root = destination.resolve()
    with tarfile.open(archive, mode="r:gz") as bundle:
        members = bundle.getmembers()
        if not members:
            raise OnboardingError("source archive is empty")
        for member in members:
            if member.name.startswith("/") or "\x00" in member.name:
                raise OnboardingError("source archive path is invalid")
            target = (destination / member.name).resolve()
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise OnboardingError(
                    "source archive escapes its staging root"
                ) from exc
            if not member.isdir() and not member.isfile():
                raise OnboardingError("source archive contains a non-regular artifact")
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(target, 0o700)
                continue
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source = bundle.extractfile(member)
            if source is None:
                raise OnboardingError("source archive member is unreadable")
            descriptor = os.open(
                target,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o700 if member.mode & 0o111 else 0o600,
            )
            try:
                with os.fdopen(descriptor, "wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
            finally:
                source.close()
    if not (destination / "pyproject.toml").is_file():
        raise OnboardingError("source archive lacks pyproject.toml")


def _run(
    argv: Sequence[str],
    *,
    env: dict[str, str] | None = None,
    timeout: int = 900,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:]
        detail = tail[0][:500] if tail else f"exit {completed.returncode}"
        raise OnboardingError(f"command failed ({Path(argv[0]).name}): {detail}")
    return completed


def install_reviewed_toolchain(
    stage: Path,
    reviewed_assets: Path,
    cache_root: Path,
) -> tuple[Path, Path, Path]:
    """Install reviewed uv, CodeGraph, and exact managed CPython into stage."""

    if not reviewed_assets.is_file() or reviewed_assets.is_symlink():
        raise OnboardingError("reviewed tool asset registry is unsafe")
    uv = stage / "tools" / "uv"
    codegraph_bundle = stage / "tools" / "codegraph"
    codegraph_link = stage / "tools" / "codegraph-link"
    cache_root.mkdir(parents=True, exist_ok=True)
    command = (
        'set -euo pipefail; . "$1"; '
        'mac_install_reviewed_uv "$2" "$3"; '
        'mac_install_reviewed_codegraph "$4" "$5" "$3"'
    )
    clean_env = {
        "HOME": str(Path.home()),
        "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", ""),
        "HTTP_PROXY": os.environ.get("HTTP_PROXY", ""),
        "NO_PROXY": os.environ.get("NO_PROXY", ""),
    }
    _run(
        (
            "/usr/bin/env",
            "bash",
            "-c",
            command,
            "_",
            str(reviewed_assets),
            str(uv),
            str(cache_root),
            str(codegraph_bundle),
            str(codegraph_link),
        ),
        env=clean_env,
    )
    uv_report = _run((str(uv), "--version"), env=clean_env).stdout.strip()
    if re.fullmatch(rf"uv {re.escape(UV_VERSION)}(?: .*)?", uv_report) is None:
        raise OnboardingError("reviewed uv version differs")
    if _run(
        (str(codegraph_bundle / "bin" / "codegraph"), "--version"), env=clean_env
    ).stdout.strip() != CODEGRAPH_VERSION.removeprefix("v"):
        raise OnboardingError("reviewed CodeGraph version differs")
    python_root = stage / "python"
    python_env = dict(clean_env)
    python_env["UV_PYTHON_INSTALL_DIR"] = str(python_root)
    python_env["UV_MANAGED_PYTHON"] = "1"
    _run(
        (
            str(uv),
            "python",
            "install",
            "--no-bin",
            "--no-registry",
            PYTHON_VERSION,
        ),
        env=python_env,
    )
    candidates = sorted(python_root.glob("*/bin/python3.12"))
    if len(candidates) != 1:
        raise OnboardingError("reviewed Python install did not yield one interpreter")
    version = _run(
        (
            str(candidates[0]),
            "-I",
            "-c",
            "import platform; print(platform.python_version())",
        ),
        env=python_env,
    ).stdout.strip()
    if version != PYTHON_VERSION:
        raise OnboardingError("reviewed Python version differs")
    return uv, codegraph_bundle, candidates[0]


def _trusted_gh() -> Path:
    search = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
    candidate = shutil.which("gh", path=search)
    if not candidate:
        raise OnboardingError("GitHub CLI is absent from the reviewed command path")
    resolved = Path(candidate).resolve()
    metadata = resolved.stat()
    if not stat.S_ISREG(metadata.st_mode) or not os.access(resolved, os.X_OK):
        raise OnboardingError("GitHub CLI is not a regular executable")
    _run((str(resolved), "--version"), env={"HOME": str(Path.home()), "PATH": search})
    return resolved


def prepare(
    layout: Layout,
    *,
    generation: str,
    agent: str,
    source_revision: str,
    supervisor: str,
    archive: Path,
    reviewed_assets: Path,
    route_identity: Path,
) -> dict[str, Any]:
    generation = _safe_generation(generation)
    if re.fullmatch(r"[0-9a-f]{40}", source_revision) is None:
        raise OnboardingError("source revision is invalid")
    with onboarding_lock(layout):
        pristine = validate_pristine(layout, supervisor)
        _, route_sha256 = validate_route_identity(route_identity)
        stage = layout.stage(generation)
        if _path_exists(stage):
            marker = stage / "stage.json"
            prior = _private_json(marker, STAGE_SCHEMA)
            if prior.get("generation") != generation or prior.get("agent") != agent:
                raise OnboardingError("existing onboarding stage has a different owner")
            shutil.rmtree(stage)
        _ensure_private_directory(stage.parent)
        stage.mkdir(mode=0o700)
        try:
            staged_archive = stage / "mac.tar.gz"
            shutil.copyfile(archive, staged_archive)
            os.chmod(staged_archive, 0o600)
            source_sha256 = _sha256(staged_archive)
            staged_source = stage / "source"
            _extract_source_archive(staged_archive, staged_source)
            cache = layout.mac_home / "cache" / "reviewed-assets"
            _ensure_private_directory(cache)
            uv, codegraph, python = install_reviewed_toolchain(
                stage, reviewed_assets, cache
            )
            gh = _trusted_gh()
            marker_value = {
                "schema": STAGE_SCHEMA,
                "status": "prepared",
                "agent": agent,
                "generation": generation,
                "source_revision": source_revision,
                "source_archive_sha256": source_sha256,
                "route_identity_sha256": route_sha256,
                "supervisor": supervisor,
                "instance_kind": "fungible",
                "versions": {
                    "uv": UV_VERSION,
                    "python": PYTHON_VERSION,
                    "codegraph": CODEGRAPH_VERSION,
                },
                "paths": {
                    "uv": str(uv.relative_to(stage)),
                    "codegraph": str(codegraph.relative_to(stage)),
                    "python": str(python.relative_to(stage)),
                    "gh": str(gh),
                },
                "pristine": pristine,
            }
            _atomic_private_json(stage / "stage.json", marker_value)
            return marker_value
        except BaseException:
            shutil.rmtree(stage, ignore_errors=True)
            raise


def _rewrite_venv_prefix(venv: Path, final_venv: Path) -> None:
    old = str(venv).encode()
    new = str(final_venv).encode()
    for item in (venv / "bin").iterdir():
        try:
            metadata = item.lstat()
        except OSError:
            continue
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_JSON_BYTES:
            continue
        raw = item.read_bytes()
        if old not in raw:
            continue
        temporary = item.with_name(f".{item.name}.rewrite")
        temporary.write_bytes(raw.replace(old, new))
        os.chmod(temporary, stat.S_IMODE(metadata.st_mode))
        os.replace(temporary, item)


def _link_in_stage(stage: Path, name: str, target: Path) -> Path:
    links = stage / "links"
    links.mkdir(exist_ok=True, mode=0o700)
    link = links / name
    link.unlink(missing_ok=True)
    link.symlink_to(target)
    return link


def _remove_created(paths: list[Path]) -> None:
    for path in reversed(paths):
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            shutil.rmtree(path)
        else:
            path.unlink()


def _publish_path(source: Path, destination: Path, created: list[Path]) -> None:
    if _path_exists(destination):
        raise OnboardingError(f"publish destination already exists: {destination}")
    _ensure_private_directory(destination.parent)
    os.replace(source, destination)
    created.append(destination)
    _fsync_directory(destination.parent)


def _validate_placeholder(
    path: Path, *, agent: str, generation: str, route_sha256: str
) -> dict[str, Any]:
    value = _private_json(path, PLACEHOLDER_SCHEMA)
    expected = {
        "agent": agent,
        "agent_id": "agent_"
        + re.sub(r"[^A-Za-z0-9_.-]+", "_", agent.lower()).strip("_"),
        "generation": generation,
        "route_identity_sha256": route_sha256,
        "instance_kind": "fungible",
        "status": "draining",
        "health_status": "degraded",
    }
    mismatches = [key for key, wanted in expected.items() if value.get(key) != wanted]
    if mismatches:
        raise OnboardingError(
            "placeholder receipt differs at: " + ",".join(sorted(mismatches))
        )
    return value


def commit(
    layout: Layout,
    *,
    generation: str,
    agent: str,
    source_revision: str,
    supervisor: str,
    placeholder: Path,
) -> dict[str, Any]:
    generation = _safe_generation(generation)
    with onboarding_lock(layout):
        pristine = validate_pristine(layout, supervisor)
        stage = layout.stage(generation)
        marker = _private_json(stage / "stage.json", STAGE_SCHEMA)
        for key, expected in (
            ("status", "prepared"),
            ("agent", agent),
            ("generation", generation),
            ("source_revision", source_revision),
            ("instance_kind", "fungible"),
        ):
            if marker.get(key) != expected:
                raise OnboardingError(f"onboarding stage differs at: {key}")
        route_sha256 = str(marker.get("route_identity_sha256") or "")
        placeholder_value = _validate_placeholder(
            placeholder,
            agent=agent,
            generation=generation,
            route_sha256=route_sha256,
        )
        paths = marker.get("paths")
        if not isinstance(paths, dict):
            raise OnboardingError("onboarding stage paths are invalid")
        staged_uv = stage / str(paths["uv"])
        staged_codegraph = stage / str(paths["codegraph"])
        staged_python = stage / str(paths["python"])
        staged_python_root = stage / "python"
        staged_source = stage / "source"
        gh = Path(str(paths["gh"]))
        final_python_root = layout.mac_home / "lib" / "python"
        relative_python = staged_python.relative_to(staged_python_root)
        final_python = final_python_root / relative_python
        final_uv = layout.mac_home / "lib" / "uv" / "versions" / UV_VERSION / "uv"
        final_codegraph = (
            layout.mac_home / "lib" / "codegraph" / "versions" / CODEGRAPH_VERSION
        )
        staged_venv = stage / "venv"
        created: list[Path] = []
        try:
            # The venv's interpreter symlink must name its final managed Python
            # home. Publish that exact runtime first, then compensate it with
            # every other generation artifact if the receipt cannot commit.
            _publish_path(staged_python_root, final_python_root, created)
            python_env = {
                "HOME": str(layout.home),
                "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                "UV_PYTHON_INSTALL_DIR": str(final_python_root),
                "UV_MANAGED_PYTHON": "1",
                "UV_PYTHON_DOWNLOADS": "never",
            }
            _run(
                (
                    str(staged_uv),
                    "venv",
                    "--python",
                    str(final_python),
                    "--managed-python",
                    "--no-python-downloads",
                    "--relocatable",
                    str(staged_venv),
                ),
                env=python_env,
            )
            _run(
                (
                    str(staged_uv),
                    "pip",
                    "install",
                    "--python",
                    str(staged_venv / "bin" / "python"),
                    "--no-config",
                    f"{staged_source}[hermes-gateway,relay]",
                ),
                env=python_env,
            )
            _rewrite_venv_prefix(staged_venv, layout.venv)
            _publish_path(staged_source, layout.source, created)
            _publish_path(staged_venv, layout.venv, created)
            _publish_path(staged_codegraph, final_codegraph, created)
            _publish_path(staged_uv, final_uv, created)
            links = (
                (
                    _link_in_stage(stage, "mac", layout.venv / "bin" / "mac"),
                    layout.mac_bin,
                ),
                (
                    _link_in_stage(
                        stage, "codegraph", final_codegraph / "bin" / "codegraph"
                    ),
                    layout.codegraph_bin,
                ),
                (_link_in_stage(stage, "gh", gh), layout.gh_bin),
            )
            for staged_link, final_link in links:
                _publish_path(staged_link, final_link, created)

            python_version = _run(
                (
                    str(layout.venv / "bin" / "python"),
                    "-I",
                    "-c",
                    "import platform; print(platform.python_version())",
                ),
                env=python_env,
            ).stdout.strip()
            if python_version != PYTHON_VERSION:
                raise OnboardingError("published venv Python version differs")
            _run((str(layout.venv / "bin" / "mac"), "--help"), env=python_env)
            if _run(
                (str(layout.codegraph_bin), "--version"), env=python_env
            ).stdout.strip() != CODEGRAPH_VERSION.removeprefix("v"):
                raise OnboardingError("published CodeGraph version differs")
            _run((str(layout.gh_bin), "--version"), env=python_env)
            receipt = {
                "schema": RECEIPT_SCHEMA,
                "status": "published",
                "agent": agent,
                "agent_id": placeholder_value["agent_id"],
                "generation": generation,
                "source_revision": source_revision,
                "source_archive_sha256": marker["source_archive_sha256"],
                "route_identity_sha256": route_sha256,
                "instance_kind": "fungible",
                "barrier": {"status": "draining", "health_status": "degraded"},
                "versions": marker["versions"],
                "paths": {
                    "source": str(layout.source),
                    "venv": str(layout.venv),
                    "python": str(final_python),
                    "uv": str(final_uv),
                    "codegraph": str(final_codegraph),
                    "mac_link": str(layout.mac_bin),
                    "codegraph_link": str(layout.codegraph_bin),
                    "gh_link": str(layout.gh_bin),
                },
                "services_started": False,
                "committed_at": dt.datetime.now(dt.timezone.utc)
                .isoformat()
                .replace("+00:00", "Z"),
                "pristine_proof": pristine,
            }
            _atomic_private_json(layout.receipt, receipt)
            # The receipt is now the durable commit marker. Staging cleanup is
            # non-authoritative and may be retried by ordinary hygiene.
            shutil.rmtree(stage, ignore_errors=True)
            return receipt
        except BaseException:
            layout.receipt.unlink(missing_ok=True)
            _remove_created(created)
            raise


def inspect(layout: Layout, *, supervisor: str) -> dict[str, Any]:
    # Deliberately do not acquire onboarding_lock here: creating the lock or
    # ~/.mac would turn cohort-wide classification into a remote mutation.
    return {
        "schema": STATUS_SCHEMA,
        "status": "eligible",
        "instance_kind": "fungible",
        "checks": validate_pristine(layout, supervisor),
    }


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare a pristine fungible node for typed MAC deployment"
    )
    parser.add_argument("action", choices=("inspect", "prepare", "commit"))
    parser.add_argument("--home", default=str(Path.home()))
    parser.add_argument("--mac-home")
    parser.add_argument("--generation")
    parser.add_argument("--agent")
    parser.add_argument("--source-revision")
    parser.add_argument(
        "--supervisor",
        choices=("auto", "systemd", "launchd", "supervisord"),
        default="auto",
    )
    parser.add_argument("--archive")
    parser.add_argument("--reviewed-assets")
    parser.add_argument("--route-identity")
    parser.add_argument("--placeholder")
    return parser.parse_args(argv)


def _required(args: argparse.Namespace, *names: str) -> None:
    missing = [
        name for name in names if not getattr(args, name.replace("-", "_"), None)
    ]
    if missing:
        raise OnboardingError("missing required arguments: " + ",".join(missing))


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])
    layout = Layout.for_home(
        Path(args.home), Path(args.mac_home) if args.mac_home else None
    )
    try:
        if args.action == "inspect":
            payload = inspect(layout, supervisor=args.supervisor)
        elif args.action == "prepare":
            _required(
                args,
                "generation",
                "agent",
                "source-revision",
                "archive",
                "reviewed-assets",
                "route-identity",
            )
            payload = prepare(
                layout,
                generation=args.generation,
                agent=args.agent,
                source_revision=args.source_revision,
                supervisor=args.supervisor,
                archive=Path(args.archive),
                reviewed_assets=Path(args.reviewed_assets),
                route_identity=Path(args.route_identity),
            )
        else:
            _required(
                args,
                "generation",
                "agent",
                "source-revision",
                "placeholder",
            )
            payload = commit(
                layout,
                generation=args.generation,
                agent=args.agent,
                source_revision=args.source_revision,
                supervisor=args.supervisor,
                placeholder=Path(args.placeholder),
            )
    except (
        OnboardingError,
        OSError,
        subprocess.SubprocessError,
        tarfile.TarError,
    ) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    sys.stdout.buffer.write(_canonical(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
