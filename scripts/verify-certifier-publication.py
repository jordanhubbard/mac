#!/usr/bin/env python3
"""Verify an immutable GHCR certifier reference and reviewed policy checksum."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


IMAGE_RE = re.compile(r"^ghcr\.io/jordanhubbard/mac-certifier@(sha256:[0-9a-f]{64})$")


class VerificationError(RuntimeError):
    pass


def policy_checksum(path: Path) -> str:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise VerificationError(f"policy is unreadable: {path}") from exc
    if not payload:
        raise VerificationError("policy is empty")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _anonymous_docker_environment(config_root: Path) -> dict[str, str]:
    config_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    config_root.chmod(0o700)
    config = config_root / "config.json"
    config.write_text('{"auths":{}}\n', encoding="utf-8")
    config.chmod(0o600)
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in {"DOCKER_AUTH_CONFIG", "DOCKER_CONFIG", "REGISTRY_AUTH_FILE"}
    }
    environment["DOCKER_CONFIG"] = str(config_root)
    return environment


def _host_linux_platform() -> str:
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "linux/amd64"
    if machine in {"aarch64", "arm64"}:
        return "linux/arm64"
    raise VerificationError(f"unsupported Docker verification architecture: {machine}")


def _run(
    argv: list[str],
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        argv,
        check=False,
        capture_output=True,
        env=environment,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).decode("utf-8", "replace")
        raise VerificationError("anonymous image verification failed: " + detail[:500])
    return completed


def verify_registry_digest(
    image_ref: str,
    *,
    expected_revision: str | None = None,
) -> dict[str, str]:
    match = IMAGE_RE.fullmatch(image_ref)
    if match is None:
        raise VerificationError(
            "image must be ghcr.io/jordanhubbard/mac-certifier@sha256:<64 lowercase hex>"
        )
    if shutil.which("docker") is None:
        raise VerificationError("docker buildx is required for registry read-back")
    if expected_revision is not None and not re.fullmatch(
        r"[0-9a-f]{40}", expected_revision
    ):
        raise VerificationError("expected revision must be an exact lowercase Git SHA")
    requested = match.group(1)
    linux_platform = _host_linux_platform()
    with tempfile.TemporaryDirectory(prefix="mac-certifier-anonymous-docker-") as raw:
        environment = _anonymous_docker_environment(Path(raw))
        manifest = _run(
            ["docker", "buildx", "imagetools", "inspect", "--raw", image_ref],
            environment=environment,
        )
        observed = "sha256:" + hashlib.sha256(manifest.stdout).hexdigest()
        if observed != requested:
            raise VerificationError(
                "registry manifest digest differs: "
                f"requested={requested} observed={observed}"
            )
        _run(
            ["docker", "pull", "--platform", linux_platform, image_ref],
            environment=environment,
        )
        inspection = _run(
            ["docker", "image", "inspect", image_ref], environment=environment
        )
        try:
            values = json.loads(inspection.stdout)
            image = values[0]
            labels = image["Config"]["Labels"]
            revision = labels["org.opencontainers.image.revision"]
            image_user = image["Config"]["User"]
            repo_digests = image["RepoDigests"]
        except (IndexError, KeyError, TypeError, json.JSONDecodeError) as exc:
            raise VerificationError(
                "pulled certifier image metadata is incomplete"
            ) from exc
        if not re.fullmatch(r"[0-9a-f]{40}", str(revision or "")):
            raise VerificationError("certifier OCI revision label is invalid")
        if expected_revision is not None and revision != expected_revision:
            raise VerificationError(
                "certifier OCI revision differs: "
                f"expected={expected_revision} observed={revision}"
            )
        if image_user != "sandbox":
            raise VerificationError("certifier image does not run as sandbox")
        if not isinstance(repo_digests, list) or image_ref not in repo_digests:
            raise VerificationError("anonymous pull did not preserve the exact digest")
        _run(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--platform",
                linux_platform,
                image_ref,
                "/opt/mac-certifier/bin/run-contract-tests",
                "--image-self-test",
            ],
            environment=environment,
        )
    return {
        "registry_digest": observed,
        "source_revision": str(revision),
        "platform": linux_platform,
        "anonymous_readback": "passed",
        "image_self_test": "passed",
    }


def _parser() -> argparse.ArgumentParser:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image_ref")
    parser.add_argument(
        "--expected-revision",
        required=True,
        help="exact tested 40-character main-branch Git SHA",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=root / "src" / "mac" / "openshell" / "default-policy.yaml",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        verification = verify_registry_digest(
            args.image_ref,
            expected_revision=args.expected_revision,
        )
        result = {
            "schema": "mac.certifier_publication_verification.v2",
            "image_ref": args.image_ref,
            **verification,
            "policy_path": str(args.policy.resolve()),
            "policy_checksum": policy_checksum(args.policy),
        }
    except VerificationError as exc:
        print(f"certifier publication verification failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
