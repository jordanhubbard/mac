from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _workflow_job(workflow: str, name: str) -> str:
    job = workflow.split(f"\n  {name}:\n", 1)[1]
    next_job = re.search(r"^  [a-z0-9][a-z0-9-]*:\s*$", job, re.MULTILINE)
    return job[: next_job.start()] if next_job is not None else job


def test_deployment_image_uses_immutable_bases_and_frozen_lock() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
    assert len(from_lines) == 3
    assert all("@sha256:" in line for line in from_lines)
    assert all(
        re.search(r"@sha256:[0-9a-f]{64}(?: AS \w+)?$", line) for line in from_lines
    )
    assert "COPY pyproject.toml uv.lock README.md ./" in dockerfile
    assert "uv sync --frozen --no-dev --no-editable" in dockerfile
    for extra in ("postgres", "k8s", "hermes-gateway"):
        assert f"--extra {extra}" in dockerfile
    assert "pip install" not in dockerfile
    assert "COPY --from=builder /opt/mac-venv /opt/mac-venv" in dockerfile
    assert "mac:x:10001:10001:" in dockerfile
    assert "groupadd" not in dockerfile
    assert "useradd" not in dockerfile


def test_deployment_build_context_excludes_secret_shaped_local_state() -> None:
    dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8")

    for pattern in (
        ".env",
        ".env.*",
        ".aws",
        ".gnupg",
        ".ssh",
        ".netrc",
        "**/credentials*",
        "**/id_ed25519*",
        "**/id_rsa*",
        "**/*.key",
        "**/*.pem",
        ".codex",
        ".tickets",
    ):
        assert pattern in dockerignore


def test_deployment_image_ci_proves_nonroot_state_volume_writes() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    docker_job = workflow.split("\n  docker:\n", 1)[1].split(
        "\n  openshell-runtime-image:\n", 1
    )[0]

    assert "type=volume,src=$state_volume,dst=/var/lib/mac" in docker_job
    assert 'test "$(id -u)" = 10001' in docker_job
    assert 'test "$(id -g)" = 10001' in docker_job
    assert ": > /var/lib/mac/mac.db" in docker_job
    assert ": > /var/lib/mac/crash-spool/nonroot-write-probe.json" in docker_job
    assert "test -x /opt/mac-venv/bin/mac-git-askpass" in docker_job


def test_all_image_publication_smokes_use_valid_docker_label_templates() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    valid_template = (
        "--format='{{ index .Config.Labels "
        '"org.opencontainers.image.revision" }}\''
    )
    invalid_template = (
        "--format='{{ index .Config.Labels "
        r'\"org.opencontainers.image.revision\" }}\''
    )

    assert invalid_template not in workflow
    for job_name in ("docker", "openshell-runtime-image", "certifier-image"):
        assert _workflow_job(workflow, job_name).count(valid_template) == 1


def test_tested_main_publishes_immutable_multiarch_openshell_runtime() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    job = workflow.split("\n  openshell-runtime-image:\n", 1)[1].split(
        "\n  certifier-image:\n", 1
    )[0]
    assert (
        "needs: [dead-code, mainline, compatibility, postgres-contract, publication-scope]"
        in job
    )
    assert "deploy/openshell/prepare-runtime-image-assets.sh" in job
    assert "file: deploy/openshell/mac-hermes.Containerfile" in job
    assert "platforms: linux/amd64,linux/arm64" in job
    assert "ghcr.io/jordanhubbard/mac-openshell-runtime:git-${{ github.sha }}" in job
    for version in (
        "GH_VERSION=2.95.0",
        "CODEGRAPH_VERSION=v1.1.6",
        "NODE_VERSION=22.23.1",
        "PNPM_VERSION=11.13.1",
        "CODEX_VERSION=0.140.0",
    ):
        assert version in job
    assert "org.opencontainers.image.source=https://github.com/jordanhubbard/mac" in job
    assert "org.opencontainers.image.revision=${{ github.sha }}" in job
    assert "provenance: mode=max" in job
    assert "sbom: true" in job
    assert "subject-digest: ${{ steps.publish.outputs.digest }}" in job
    assert "openshell-runtime-image-publication" in job
    assert "/user/packages/container/mac-openshell-runtime/visibility" not in job
    assert "gh api --method PATCH" not in job
    assert "gh api /user/packages/container/mac-openshell-runtime" in job
    assert 'test "$visibility" = public' in job
    assert "printf '{\"auths\":{}}\\n'" in job
    assert 'DOCKER_CONFIG="$empty_config" docker pull' in job
    assert "for platform in linux/amd64 linux/arm64" in job
    assert 'docker run --rm --platform "$platform"' in job
    assert "org.opencontainers.image.revision" in job
    for command in (
        "gh --version",
        "codex --version",
        "codegraph --version",
        "clang --version",
        "llvm-objcopy --version",
        "ld.lld --version",
        "qemu-system-riscv64 --version",
    ):
        assert command in job
    assert "clang --print-targets | grep -F riscv64" in job
    assert "qemu-system-riscv64 -machine help | grep -F virt" in job


def test_image_publication_is_blocked_on_live_pinned_postgres_contract() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    postgres = workflow.split("\n  postgres-contract:\n", 1)[1].split(
        "\n  nightly:\n", 1
    )[0]
    assert (
        "docker.io/library/postgres@sha256:"
        "33f923b05f64ca54ac4401c01126a6b92afe839a0aa0a52bc5aeb5cc958e5f20"
    ) in postgres
    assert "MAC_TEST_PG_URL: postgresql://" in postgres
    assert "pytest -q -m postgres tests/test_postgres_live.py" in postgres
    assert (
        workflow.count(
            "needs: [dead-code, mainline, compatibility, postgres-contract, publication-scope]"
        )
        == 3
    )


def test_manual_image_publication_is_restricted_to_protected_main() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    manual_guard = (
        "(github.event_name == 'workflow_dispatch' && "
        "github.ref == 'refs/heads/main' && inputs.publish_images)"
    )

    for job_name in (
        "mainline",
        "compatibility",
        "postgres-contract",
        "publication-scope",
        "docker",
        "openshell-runtime-image",
    ):
        assert manual_guard in _workflow_job(workflow, job_name)

    certifier_job = _workflow_job(workflow, "certifier-image")
    assert "github.ref == 'refs/heads/main'" in certifier_job
    assert (
        "needs.publication-scope.outputs.certifier_changed == 'true'" in certifier_job
    )


def test_main_deployment_publication_is_anonymously_executable_on_both_arches() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  docker:\n", 1)[1].split(
        "\n  openshell-runtime-image:\n", 1
    )[0]

    assert "gh api --method PATCH" not in job
    assert "gh api /user/packages/container/mac" in job
    assert 'test "$visibility" = public' in job
    assert 'exact_ref="ghcr.io/jordanhubbard/mac@$IMAGE_DIGEST"' in job
    assert "printf '{\"auths\":{}}\\n'" in job
    assert "for platform in linux/amd64 linux/arm64" in job
    assert 'DOCKER_CONFIG="$empty_config" docker pull --platform "$platform"' in job
    assert "--network none" in job
    assert '--platform "$platform" --entrypoint /bin/sh' in job
    assert 'test "$(id -u)" = 10001' in job
    assert "test -x /opt/mac-venv/bin/mac-git-askpass" in job
    assert (
        "import cryptography, fastapi, kubernetes, mac.api, psycopg, uvicorn, yaml"
        in job
    )


def test_all_publishers_pin_qemu_before_buildx() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    qemu = "docker/setup-qemu-action@c7c53464625b32c7a7e944ae62b3e17d2b600130"
    buildx = "docker/setup-buildx-action@8d2750c68a42422c14e847fe6c8ac0403b4cbd6f"

    assert workflow.count(qemu) == 3
    for job_name, next_job in (
        ("docker", "openshell-runtime-image"),
        ("openshell-runtime-image", "certifier-image"),
        ("certifier-image", None),
    ):
        job = workflow.split(f"\n  {job_name}:\n", 1)[1]
        if next_job is not None:
            job = job.split(f"\n  {next_job}:\n", 1)[0]
        assert job.index(qemu) < job.index(buildx)
        assert "platforms: arm64" in job
