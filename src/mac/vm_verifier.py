"""Controller transport for explicitly selected dedicated VM verification."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tempfile
from typing import Any
import uuid

from .vm_verifier_host import MAX_ARCHIVE, SCHEMA, digest, private_config


def configured_vm_verifier(remote_url: str) -> dict[str, Any] | None:
    path = os.environ.get("MAC_HUB_VERIFY_VM_CONFIG", "").strip()
    if not path:
        return None
    config = private_config(Path(path))
    repositories = config.get("repositories")
    if (
        not isinstance(repositories, list)
        or not repositories
        or not all(isinstance(value, str) and value for value in repositories)
    ):
        raise ValueError("VM verifier requires an explicit repository allowlist")
    if remote_url not in repositories:
        return None
    for key in ("base_sha256", "firmware_code_sha256", "firmware_vars_sha256"):
        if not re.fullmatch(r"[0-9a-f]{64}", str(config.get(key, ""))):
            raise ValueError(f"VM verifier requires a pinned {key}")
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*", str(config.get("ssh_target", ""))):
        raise ValueError("invalid VM verifier SSH target")
    for key in ("ssh_identity", "ssh_known_hosts", "remote_runner", "remote_config"):
        if not isinstance(config.get(key), str) or not Path(config[key]).is_absolute():
            raise ValueError(f"VM verifier requires absolute {key}")
    config["controller_config_sha256"] = digest(Path(path))
    return config


def run_staged_vm_verification(
    config: dict[str, Any],
    archive: Path,
    *,
    remote_url: str,
    head_sha: str,
    tree_sha: str,
    test_command: str,
    bootstrap_command: str,
    timeout_seconds: float,
    verifier_identity: dict[str, Any] | None = None,
) -> tuple[int, str]:
    size = archive.stat().st_size
    if not 0 < size <= MAX_ARCHIVE:
        return 1, "hub verification is unavailable: VM source archive exceeds limit"
    request = {
        "schema": SCHEMA,
        "nonce": uuid.uuid4().hex,
        "remote_url": remote_url,
        "head_sha": head_sha,
        "tree_sha": tree_sha,
        "archive_size": size,
        "archive_sha256": digest(archive),
        "base_sha256": config["base_sha256"],
        "test_command": test_command,
        "bootstrap_command": bootstrap_command,
        "timeout_seconds": max(1, int(timeout_seconds)),
    }
    remote_command = shlex.join(
        [
            "/usr/bin/python3",
            config["remote_runner"],
            "--config",
            config["remote_config"],
        ]
    )
    argv = [
        "ssh",
        "-F",
        "/dev/null",
        "-o",
        "BatchMode=yes",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ForwardAgent=no",
        "-o",
        "ConnectTimeout=15",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        f"UserKnownHostsFile={config['ssh_known_hosts']}",
        "-i",
        config["ssh_identity"],
        config["ssh_target"],
        remote_command,
    ]
    try:
        with tempfile.TemporaryFile() as payload:
            payload.write(json.dumps(request).encode() + b"\n")
            with archive.open("rb") as source:
                shutil.copyfileobj(source, payload)
            payload.seek(0)
            result = subprocess.run(
                argv, stdin=payload, capture_output=True, timeout=timeout_seconds + 30, check=False
            )
        if result.returncode:
            return (
                1,
                "hub verification is unavailable: dedicated VM transport/runner failed\n"
                + result.stderr.decode(errors="replace")[-4000:],
            )
        response = json.loads(result.stdout)
        for key in (
            "schema",
            "nonce",
            "head_sha",
            "tree_sha",
            "base_sha256",
            "archive_sha256",
            "test_command",
            "bootstrap_command",
        ):
            if response.get(key) != request[key]:
                raise ValueError(f"VM verifier response identity mismatch: {key}")
        if (
            response.get("execution_environment") != "dedicated_kvm"
            or response.get("sanitizer_controls") != "clean-pass,leak-rejected"
            or response.get("execution_attempted") is not True
        ):
            raise ValueError("VM verifier lacks full sanitizer qualification")
        for key in ("firmware_code_sha256", "firmware_vars_sha256"):
            if response.get(key) != config[key]:
                raise ValueError(f"VM verifier response identity mismatch: {key}")
        rc = response.get("returncode")
        if type(rc) is not int or not -255 <= rc <= 255:
            raise ValueError("invalid VM verifier returncode")
        if not isinstance(response.get("output"), str):
            raise ValueError("VM verifier omitted test output")
        if verifier_identity is not None:
            verifier_identity.update(
                {key: value for key, value in response.items() if key != "output"}
            )
            verifier_identity["controller_config_sha256"] = config["controller_config_sha256"]
        return rc, response["output"]
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        return 1, f"hub verification is unavailable: dedicated VM verification failed: {exc}"
