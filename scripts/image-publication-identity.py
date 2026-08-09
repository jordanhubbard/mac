#!/usr/bin/env python3
"""Plan, reuse, verify, and receipt repository-owned OCI image publications.

The expensive image is keyed by the files that can actually reach its final
filesystem plus its reviewed build arguments.  A controller commit is recorded
separately from the revision that originally built a reused digest; conflating
those identities forces a rebuild for documentation-only commits and makes
provenance claims less truthful, not more.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from typing import Any, Iterable


PLAN_SCHEMA = "mac.image_publication_plan.v1"
VERIFY_SCHEMA = "mac.image_anonymous_verification.v1"
RECEIPT_SCHEMA = "mac.image_publication_identity.v1"
SOURCE = "https://github.com/jordanhubbard/mac"
GITHUB_REPOSITORY = "jordanhubbard/mac"
PLATFORMS = ("linux/amd64", "linux/arm64")
SHA40 = re.compile(r"[0-9a-f]{40}\Z")
SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_INPUT_FILE_BYTES = 128 * 1024 * 1024
MAX_INPUT_TOTAL_BYTES = 512 * 1024 * 1024

IMAGE_SPECS = {
    "mac": {
        "repository": "ghcr.io/jordanhubbard/mac",
        "files": (
            ".dockerignore",
            "Dockerfile",
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "deploy/mac-crash-observer.py",
        ),
        "trees": ("src",),
        "build_args": {},
    },
    "openshell-runtime": {
        "repository": "ghcr.io/jordanhubbard/mac-openshell-runtime",
        "files": (
            ".dockerignore",
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "deploy/openshell/mac-hermes.Containerfile",
            # The BOM derived from every repository contract. Frozen here so a
            # contract gaining a tool changes the image identity: the sandbox is
            # the security boundary, and "which tools are in it" must be part of
            # what the published digest attests, not a fact kept alongside it.
            "deploy/openshell/sandbox-bom.json",
            "deploy/openshell/prepare-runtime-image-assets.sh",
            "deploy/reviewed-tool-assets.sh",
            "deploy/verify-bash-contract.sh",
        ),
        "trees": ("src",),
        "build_args": {
            "BUILDX_VERSION": "0.30.1",
            "CODEGRAPH_VERSION": "v1.1.6",
            "CODEX_VERSION": "0.140.0",
            "CLAUDE_VERSION": "2.1.220",
            "CURSOR_VERSION": "2026.07.23-e383d2b",
            "GH_VERSION": "2.95.0",
            "NODE_VERSION": "22.23.1",
            "PNPM_VERSION": "11.13.1",
        },
    },
}


class IdentityError(ValueError):
    """An image identity or its evidence is unsafe or inconsistent."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def sha256(raw: bytes) -> str:
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def utc_now() -> str:
    return (
        dt.datetime.now(dt.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _ignored_copied_path(relative: Path) -> bool:
    """Mirror the exclusions that can affect the repository-owned COPY trees.

    Both Dockerfiles COPY ``src`` explicitly.  The root .dockerignore excludes
    these secret/generated shapes after re-including that tree.  Keeping this
    small matcher beside the fingerprint schema makes the frozen-input boundary
    reviewable and testable without asking BuildKit to execute a build first.
    """

    parts = relative.parts
    basename = relative.name
    if any(
        part
        in {
            ".venv",
            "__pycache__",
            ".pytest_cache",
            "dist",
            ".git",
            ".aws",
            ".gnupg",
            ".ssh",
            ".codex",
            ".tickets",
            ".codegraph",
            ".test-portfolio",
            "htmlcov",
            ".ruff_cache",
            ".mypy_cache",
            ".tox",
            ".vendor-check-work",
        }
        for part in parts
    ):
        return True
    if (
        len(parts) >= 2
        and ".beads" in parts
        and basename
        in {
            "backup",
            "embeddeddolt",
        }
    ):
        return True
    if basename in {
        ".coverage",
        ".mac-openshell-build.lock",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "coverage.json",
        "coverage.xml",
    }:
        return True
    if basename == ".env" or basename.startswith(".env."):
        return True
    if basename.endswith((".pyc", ".db", ".sqlite", ".key", ".p12", ".pem", ".pfx")):
        return True
    if basename.endswith(".md"):
        return True
    if basename.startswith(("credentials", "id_ed25519", "id_rsa")):
        return True
    return False


def _entry(root: Path, path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IdentityError(f"frozen image input is unreadable: {path}") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > MAX_INPUT_FILE_BYTES
        ):
            raise IdentityError(
                f"frozen image input is not a bounded regular file: {path}"
            )
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise IdentityError(f"frozen image input changed while reading: {path}")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise IdentityError(f"frozen image input grew while reading: {path}")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mode,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mode,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise IdentityError(f"frozen image input changed while reading: {path}")
        return {
            "path": path.relative_to(root).as_posix(),
            "mode": format(stat.S_IMODE(before.st_mode), "04o"),
            "size": len(raw),
            "sha256": sha256(bytes(raw)),
        }
    finally:
        os.close(descriptor)


def frozen_entries(root: Path, kind: str) -> list[dict[str, Any]]:
    try:
        spec = IMAGE_SPECS[kind]
    except KeyError as exc:
        raise IdentityError("unsupported image kind") from exc
    paths: set[Path] = set()
    for raw in spec["files"]:
        path = root / raw
        if not path.is_file() or path.is_symlink():
            raise IdentityError(f"required frozen image input is absent: {raw}")
        paths.add(path)
    for raw in spec["trees"]:
        tree = root / raw
        if not tree.is_dir() or tree.is_symlink():
            raise IdentityError(f"required frozen image input tree is absent: {raw}")
        for path in tree.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            relative = path.relative_to(root)
            if not _ignored_copied_path(relative):
                paths.add(path)
    # ``Path`` orders by path components, while the signed plan validates and
    # hashes POSIX path strings.  Those orderings differ when one sibling name
    # is a prefix of another (for example ``openai/`` and ``openai-codex/``).
    # Sort by the exact serialized representation so every plan produced here
    # is immediately acceptable to the independent plan validator.
    ordered_paths = sorted(paths, key=lambda path: path.relative_to(root).as_posix())
    entries = [_entry(root, path) for path in ordered_paths]
    if sum(entry["size"] for entry in entries) > MAX_INPUT_TOTAL_BYTES:
        raise IdentityError("frozen image inputs exceed the reviewed total size bound")
    return entries


def parse_build_args(values: Iterable[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        name, separator, value = raw.partition("=")
        if (
            not separator
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", name)
            or not value
            or len(value.encode()) > 256
            or any(character.isspace() for character in value)
            or name in result
        ):
            raise IdentityError("image build argument is malformed or duplicated")
        result[name] = value
    return dict(sorted(result.items()))


def build_plan(
    root: Path, kind: str, requested_revision: str, build_args: Iterable[str]
) -> dict[str, Any]:
    if not SHA40.fullmatch(requested_revision):
        raise IdentityError("requested revision must be an exact Git SHA")
    entries = frozen_entries(root, kind)
    parsed_build_args = parse_build_args(build_args)
    if parsed_build_args != IMAGE_SPECS[kind]["build_args"]:
        raise IdentityError("image build arguments differ from the reviewed contract")
    material = {
        "schema": "mac.image_frozen_inputs.v1",
        "kind": kind,
        "platforms": list(PLATFORMS),
        "build_args": parsed_build_args,
        "files": entries,
    }
    digest = sha256(canonical(material))
    repository = str(IMAGE_SPECS[kind]["repository"])
    return {
        "schema": PLAN_SCHEMA,
        "kind": kind,
        "repository": repository,
        "requested_revision": requested_revision,
        "frozen_inputs_sha256": digest,
        "content_tag": repository + ":inputs-" + digest.removeprefix("sha256:"),
        "platforms": list(PLATFORMS),
        "build_args": material["build_args"],
        "inputs": entries,
        "planned_at": utc_now(),
    }


def _private_bytes(path: Path, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise IdentityError(f"{label} is unreadable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > MAX_JSON_BYTES
        ):
            raise IdentityError(f"{label} is not an owner-private bounded file")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(1024 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise IdentityError(f"{label} changed while reading")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise IdentityError(f"{label} grew while reading")
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
            raise IdentityError(f"{label} changed while reading")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _private_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(_private_bytes(path, label))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise IdentityError(f"{label} root is not an object")
    return value


def _private_json_value(path: Path, label: str) -> Any:
    try:
        return json.loads(_private_bytes(path, label))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise IdentityError(f"{label} is not valid JSON") from exc


def atomic_private_json(path: Path, value: dict[str, Any]) -> None:
    destination = path.expanduser().absolute()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = destination.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or stat.S_ISLNK(parent.st_mode)
        or parent.st_uid != os.getuid()
        or stat.S_IMODE(parent.st_mode) & 0o077
    ):
        raise IdentityError("image receipt directory is not owner-private")
    try:
        current = destination.lstat()
    except FileNotFoundError:
        current = None
    if current is not None and (
        not stat.S_ISREG(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or current.st_uid != os.getuid()
        or current.st_nlink != 1
    ):
        raise IdentityError("existing image receipt path is unsafe")
    descriptor, raw = tempfile.mkstemp(
        prefix=destination.name + ".", dir=destination.parent
    )
    temporary = Path(raw)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(canonical(value) + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _run(
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    allow_failure: bool = False,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    if completed.returncode != 0 and not allow_failure:
        detail = (completed.stderr or completed.stdout)[-2000:]
        raise IdentityError(f"image identity command failed: {argv[0]}: {detail}")
    return completed


def _inspect(docker: str, reference: str) -> dict[str, Any]:
    completed = _run([docker, "image", "inspect", reference])
    try:
        values = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise IdentityError("Docker image inspection was not JSON") from exc
    if (
        not isinstance(values, list)
        or len(values) != 1
        or not isinstance(values[0], dict)
    ):
        raise IdentityError("Docker image inspection shape is invalid")
    return values[0]


def _repo_digest(image: dict[str, Any], repository: str) -> str:
    prefix = repository + "@"
    values = image.get("RepoDigests")
    matches = [
        item[len(prefix) :]
        for item in values or []
        if isinstance(item, str) and item.startswith(prefix)
    ]
    if len(set(matches)) != 1 or not matches or not SHA256.fullmatch(matches[0]):
        raise IdentityError("Docker image lacks one exact repository digest")
    return matches[0]


def _labels(image: dict[str, Any]) -> dict[str, str]:
    labels = (image.get("Config") or {}).get("Labels")
    if not isinstance(labels, dict) or any(
        not isinstance(key, str) or not isinstance(value, str)
        for key, value in labels.items()
    ):
        raise IdentityError("Docker image labels are missing or malformed")
    return labels


def _validate_image(
    image: dict[str, Any],
    plan: dict[str, Any],
    *,
    platform: str,
    expected_revision: str | None = None,
) -> tuple[str, str]:
    os_kind, architecture = platform.split("/", 1)
    if image.get("Os") != os_kind or image.get("Architecture") != architecture:
        raise IdentityError("Docker image platform differs from the requested platform")
    labels = _labels(image)
    revision = labels.get("org.opencontainers.image.revision", "")
    if (
        labels.get("org.opencontainers.image.source") != SOURCE
        or labels.get("io.mac.frozen-inputs.sha256") != plan["frozen_inputs_sha256"]
        or labels.get("io.mac.image-kind") != plan["kind"]
        or not SHA40.fullmatch(revision)
        or (expected_revision is not None and revision != expected_revision)
    ):
        raise IdentityError(
            "Docker image provenance labels differ from the frozen plan"
        )
    return _repo_digest(image, plan["repository"]), revision


def _anonymous_environment() -> tuple[tempfile.TemporaryDirectory[str], dict[str, str]]:
    directory = tempfile.TemporaryDirectory(prefix="mac-image-anonymous.")
    config = Path(directory.name)
    (config / "config.json").write_text('{"auths":{}}\n', encoding="utf-8")
    os.chmod(config / "config.json", 0o600)
    environment = dict(os.environ)
    environment["DOCKER_CONFIG"] = str(config)
    return directory, environment


def probe_reuse(plan: dict[str, Any], docker: str) -> dict[str, str]:
    _validate_plan(plan)
    directory, environment = _anonymous_environment()
    try:
        completed = _run(
            [docker, "pull", "--platform", "linux/amd64", plan["content_tag"]],
            env=environment,
            allow_failure=True,
        )
        if completed.returncode != 0:
            detail = (completed.stderr + "\n" + completed.stdout).lower()
            if any(token in detail for token in ("manifest unknown", "name unknown")):
                return {"reuse": "false", "digest": "", "build_revision": ""}
            raise IdentityError("could not determine whether the frozen image exists")
        image = _inspect(docker, plan["content_tag"])
        digest, revision = _validate_image(image, plan, platform="linux/amd64")
        return {"reuse": "true", "digest": digest, "build_revision": revision}
    finally:
        directory.cleanup()


def qualify_reuse(
    plan: dict[str, Any],
    *,
    candidate_reuse: str,
    image_digest: str,
    build_revision: str,
    github_repository: str,
    gh: str,
) -> dict[str, str]:
    """Admit a content-tag hit only when its original provenance still verifies.

    A canceled workflow may have pushed the content tag immediately before it
    was terminated and therefore before the separate GitHub attestation step.
    Such an image is a cache candidate, not a reusable publication identity.
    Returning a miss lets this run rebuild/republish and repair the attestation.
    """

    _validate_plan(plan)
    miss = {"reuse": "false", "digest": "", "build_revision": ""}
    if candidate_reuse != "true":
        return miss
    if (
        github_repository != GITHUB_REPOSITORY
        or not SHA256.fullmatch(image_digest)
        or not SHA40.fullmatch(build_revision)
    ):
        raise IdentityError("reusable image candidate identity is malformed")
    completed = _run(
        [
            gh,
            "attestation",
            "verify",
            f"oci://{plan['repository']}@{image_digest}",
            "--repo",
            github_repository,
            "--signer-workflow",
            f"{github_repository}/.github/workflows/ci.yml",
            "--source-digest",
            build_revision,
            "--source-ref",
            "refs/heads/main",
            "--deny-self-hosted-runners",
            "--format",
            "json",
        ],
        allow_failure=True,
    )
    if completed.returncode != 0:
        return miss
    try:
        evidence = json.loads(completed.stdout)
        _validate_provenance_verification(evidence, image_digest, plan["repository"])
    except (UnicodeError, json.JSONDecodeError, IdentityError):
        return miss
    return {
        "reuse": "true",
        "digest": image_digest,
        "build_revision": build_revision,
    }


def _smoke_argv(kind: str, docker: str, reference: str, platform: str) -> list[str]:
    if kind == "mac":
        command = (
            'test "$(id -u)" = 10001; test "$(id -g)" = 10001; '
            "test -x /usr/local/bin/mac-crash-observer; "
            "test -x /opt/mac-venv/bin/mac-git-askpass; "
            'python -c "import cryptography, fastapi, kubernetes, mac.api, psycopg, uvicorn, yaml"'
        )
        return [
            docker,
            "run",
            "--rm",
            "--network",
            "none",
            "--platform",
            platform,
            "--entrypoint",
            "/bin/sh",
            reference,
            "-ec",
            command,
        ]
    command = (
        "set -euo pipefail; /usr/local/bin/mac-verify-bash-contract; "
        'test "$(node --version)" = v22.23.1; '
        'test "$(pnpm --version)" = 11.13.1; '
        "gh --version | head -n1 | grep -F 'gh version 2.95.0'; "
        "codex --version | grep -E '(^| )0\\.140\\.0$'; "
        "claude --version | grep -F '2.1.220'; "
        "cursor-agent --version | grep -F '2026.07.23-e383d2b'; "
        "codegraph --version | grep -Fx '1.1.6'; clang --version; "
        "clang --print-targets | grep -F riscv64; llvm-objcopy --version; "
        "ld.lld --version; qemu-system-riscv64 --version; "
        "qemu-system-riscv64 -machine help | grep -F virt; python3 --version; "
        "/usr/local/lib/docker/cli-plugins/docker-buildx version | grep -F 'v0.30.1'; "
        "getent passwd sandbox; test -x /opt/mac-venv/bin/mac-git-askpass"
    )
    return [
        docker,
        "run",
        "--rm",
        "--network",
        "none",
        "--platform",
        platform,
        "--entrypoint",
        "/bin/bash",
        reference,
        "-lc",
        command,
    ]


def verify_image(
    plan: dict[str, Any], docker: str, digest: str, build_revision: str
) -> dict[str, Any]:
    _validate_plan(plan)
    if not SHA256.fullmatch(digest) or not SHA40.fullmatch(build_revision):
        raise IdentityError("exact image digest or build revision is malformed")
    reference = plan["repository"] + "@" + digest
    directory, environment = _anonymous_environment()
    evidence: list[dict[str, Any]] = []
    try:
        for platform in plan["platforms"]:
            _run([docker, "image", "rm", "-f", reference], allow_failure=True)
            _run([docker, "pull", "--platform", platform, reference], env=environment)
            image = _inspect(docker, reference)
            observed_digest, observed_revision = _validate_image(
                image, plan, platform=platform, expected_revision=build_revision
            )
            if observed_digest != digest:
                raise IdentityError("anonymous pull resolved a different image digest")
            _run(
                _smoke_argv(plan["kind"], docker, reference, platform), env=environment
            )
            evidence.append(
                {
                    "platform": platform,
                    "digest": observed_digest,
                    "build_revision": observed_revision,
                    "smoke": "passed",
                }
            )
    finally:
        directory.cleanup()
    return {
        "schema": VERIFY_SCHEMA,
        "status": "passed",
        "anonymous": True,
        "kind": plan["kind"],
        "repository": plan["repository"],
        "image_ref": reference,
        "image_digest": digest,
        "build_revision": build_revision,
        "frozen_inputs_sha256": plan["frozen_inputs_sha256"],
        "platforms": evidence,
        "verified_at": utc_now(),
    }


def publication_receipt(
    plan: dict[str, Any],
    verification: dict[str, Any],
    provenance_verification: Any,
    disposition: str,
    provenance: str,
) -> dict[str, Any]:
    _validate_plan(plan)
    if disposition not in {"built", "reused"}:
        raise IdentityError("publication disposition is unsupported")
    if provenance not in {"github-attested", "github-attestation-verified"}:
        raise IdentityError("publication provenance result is unsupported")
    expected_provenance = (
        "github-attested" if disposition == "built" else "github-attestation-verified"
    )
    if provenance != expected_provenance:
        raise IdentityError("publication provenance does not match its disposition")
    if (
        verification.get("schema") != VERIFY_SCHEMA
        or verification.get("status") != "passed"
        or verification.get("anonymous") is not True
        or verification.get("kind") != plan["kind"]
        or verification.get("repository") != plan["repository"]
        or verification.get("frozen_inputs_sha256") != plan["frozen_inputs_sha256"]
        or not SHA256.fullmatch(str(verification.get("image_digest") or ""))
        or not SHA40.fullmatch(str(verification.get("build_revision") or ""))
        or verification.get("image_ref")
        != plan["repository"] + "@" + str(verification.get("image_digest") or "")
    ):
        raise IdentityError("anonymous verification differs from the frozen plan")
    digest = verification["image_digest"]
    build_revision = verification["build_revision"]
    platform_evidence = verification.get("platforms")
    if (
        not isinstance(platform_evidence, list)
        or [item.get("platform") for item in platform_evidence] != plan["platforms"]
        or any(
            not isinstance(item, dict)
            or item.get("digest") != digest
            or item.get("build_revision") != build_revision
            or item.get("smoke") != "passed"
            for item in platform_evidence
        )
    ):
        raise IdentityError("anonymous platform evidence differs from the frozen plan")
    if disposition == "built" and build_revision != plan["requested_revision"]:
        raise IdentityError("a newly built image must bind the controller revision")
    _validate_provenance_verification(
        provenance_verification, digest, plan["repository"]
    )
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "passed",
        "disposition": disposition,
        "kind": plan["kind"],
        "repository": plan["repository"],
        "requested_revision": plan["requested_revision"],
        "build_revision": build_revision,
        "frozen_inputs_sha256": plan["frozen_inputs_sha256"],
        "image_digest": digest,
        "image_ref": plan["repository"] + "@" + digest,
        "content_tag": plan["content_tag"],
        "platforms": plan["platforms"],
        "anonymous_verification_sha256": sha256(canonical(verification)),
        "provenance": {
            "status": "passed",
            "mode": provenance,
            "subject_digest": digest,
            "build_revision": build_revision,
            "verification_sha256": sha256(canonical(provenance_verification)),
            "verification_policy": {
                "repository": GITHUB_REPOSITORY,
                "signer_workflow": f"{GITHUB_REPOSITORY}/.github/workflows/ci.yml",
                "source_digest": build_revision,
                "source_ref": "refs/heads/main",
                "predicate_type": "https://slsa.dev/provenance/v1",
                "deny_self_hosted_runners": True,
            },
        },
        "recorded_at": utc_now(),
    }


def _validate_plan(plan: dict[str, Any]) -> None:
    kind = plan.get("kind")
    if (
        plan.get("schema") != PLAN_SCHEMA
        or kind not in IMAGE_SPECS
        or plan.get("repository") != IMAGE_SPECS[kind]["repository"]
        or not SHA40.fullmatch(str(plan.get("requested_revision") or ""))
        or not SHA256.fullmatch(str(plan.get("frozen_inputs_sha256") or ""))
        or plan.get("platforms") != list(PLATFORMS)
        or plan.get("content_tag")
        != plan["repository"]
        + ":inputs-"
        + plan["frozen_inputs_sha256"].removeprefix("sha256:")
        or not isinstance(plan.get("inputs"), list)
        or not plan["inputs"]
        or plan.get("build_args") != IMAGE_SPECS.get(str(kind), {}).get("build_args")
    ):
        raise IdentityError("image publication plan is invalid")
    paths: list[str] = []
    for item in plan["inputs"]:
        if (
            not isinstance(item, dict)
            or set(item) != {"path", "mode", "size", "sha256"}
            or not isinstance(item.get("path"), str)
            or not item["path"]
            or item["path"].startswith(("/", "../"))
            or "/../" in item["path"]
            or not re.fullmatch(r"[0-7]{4}", str(item.get("mode") or ""))
            or not isinstance(item.get("size"), int)
            or item["size"] < 0
            or not SHA256.fullmatch(str(item.get("sha256") or ""))
        ):
            raise IdentityError("image publication plan has malformed frozen inputs")
        paths.append(item["path"])
    if paths != sorted(set(paths)):
        raise IdentityError("image publication plan has duplicate or unsorted inputs")
    material = {
        "schema": "mac.image_frozen_inputs.v1",
        "kind": kind,
        "platforms": plan["platforms"],
        "build_args": plan["build_args"],
        "files": plan["inputs"],
    }
    if sha256(canonical(material)) != plan["frozen_inputs_sha256"]:
        raise IdentityError("image publication plan digest does not bind its inputs")


def _validate_provenance_verification(value: Any, digest: str, repository: str) -> None:
    if not isinstance(value, list) or not value:
        raise IdentityError("GitHub provenance verification is empty")
    expected = digest.removeprefix("sha256:")
    for item in value:
        if not isinstance(item, dict):
            continue
        result = item.get("verificationResult")
        statement = result.get("statement") if isinstance(result, dict) else None
        if (
            not isinstance(statement, dict)
            or statement.get("predicateType") != "https://slsa.dev/provenance/v1"
        ):
            continue
        subjects = statement.get("subject") if isinstance(statement, dict) else None
        if not isinstance(subjects, list):
            continue
        for subject in subjects:
            digests = subject.get("digest") if isinstance(subject, dict) else None
            if (
                subject.get("name") == repository
                and isinstance(digests, dict)
                and digests.get("sha256") == expected
            ):
                return
    raise IdentityError("GitHub provenance verification does not bind the image digest")


def _github_outputs(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    with open(path, "a", encoding="utf-8") as stream:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise IdentityError("GitHub output value is unsafe")
            stream.write(f"{key}={value}\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    plan = subparsers.add_parser("plan")
    plan.add_argument("--kind", choices=sorted(IMAGE_SPECS), required=True)
    plan.add_argument("--root", type=Path, default=Path.cwd())
    plan.add_argument("--requested-revision", required=True)
    plan.add_argument("--build-arg", action="append", default=[])
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--github-output")
    probe = subparsers.add_parser("probe")
    probe.add_argument("--plan", type=Path, required=True)
    probe.add_argument("--docker", default="docker")
    probe.add_argument("--github-output")
    qualify = subparsers.add_parser("qualify-reuse")
    qualify.add_argument("--plan", type=Path, required=True)
    qualify.add_argument("--candidate-reuse", required=True)
    qualify.add_argument("--image-digest", default="")
    qualify.add_argument("--build-revision", default="")
    qualify.add_argument("--github-repository", required=True)
    qualify.add_argument("--gh", default="gh")
    qualify.add_argument("--github-output")
    verify = subparsers.add_parser("verify")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--docker", default="docker")
    verify.add_argument("--image-digest", required=True)
    verify.add_argument("--build-revision", required=True)
    verify.add_argument("--output", type=Path, required=True)
    receipt = subparsers.add_parser("receipt")
    receipt.add_argument("--plan", type=Path, required=True)
    receipt.add_argument("--verification", type=Path, required=True)
    receipt.add_argument("--provenance-verification", type=Path, required=True)
    receipt.add_argument("--disposition", choices=("built", "reused"), required=True)
    receipt.add_argument(
        "--provenance",
        choices=("github-attested", "github-attestation-verified"),
        required=True,
    )
    receipt.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.action == "plan":
            value = build_plan(
                args.root.resolve(), args.kind, args.requested_revision, args.build_arg
            )
            atomic_private_json(args.output, value)
            _github_outputs(
                args.github_output,
                {
                    "frozen_inputs_sha256": value["frozen_inputs_sha256"],
                    "content_tag": value["content_tag"],
                    "repository": value["repository"],
                },
            )
        elif args.action == "probe":
            value = probe_reuse(_private_json(args.plan, "image plan"), args.docker)
            _github_outputs(args.github_output, value)
        elif args.action == "qualify-reuse":
            value = qualify_reuse(
                _private_json(args.plan, "image plan"),
                candidate_reuse=args.candidate_reuse,
                image_digest=args.image_digest,
                build_revision=args.build_revision,
                github_repository=args.github_repository,
                gh=args.gh,
            )
            _github_outputs(args.github_output, value)
        elif args.action == "verify":
            value = verify_image(
                _private_json(args.plan, "image plan"),
                args.docker,
                args.image_digest,
                args.build_revision,
            )
            atomic_private_json(args.output, value)
        else:
            value = publication_receipt(
                _private_json(args.plan, "image plan"),
                _private_json(args.verification, "image verification"),
                _private_json_value(
                    args.provenance_verification,
                    "GitHub provenance verification",
                ),
                args.disposition,
                args.provenance,
            )
            atomic_private_json(args.output, value)
    except IdentityError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
