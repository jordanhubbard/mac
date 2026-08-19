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
    # `hermes-gateway` was dropped from pyproject with the vendored Hermes
    # runtime in #377. This assertion outlived it and pinned the broken state:
    # it demanded the Dockerfile request an extra that no longer exists, while
    # the actual image build failed with "Extra `hermes-gateway` is not defined
    # in the project's optional-dependencies table". Two gates asserting
    # opposite things is why main stayed red.
    for extra in ("postgres", "k8s"):
        assert f"--extra {extra}" in dockerfile
    assert "--extra hermes-gateway" not in dockerfile
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
        "--format='{{ index .Config.Labels \"org.opencontainers.image.revision\" }}'"
    )
    invalid_template = (
        "--format='{{ index .Config.Labels "
        r"\"org.opencontainers.image.revision\" }}\'"
    )

    assert invalid_template not in workflow
    assert _workflow_job(workflow, "certifier-image").count(valid_template) == 1
    for job_name in ("docker", "openshell-runtime-image"):
        job = _workflow_job(workflow, job_name)
        assert valid_template not in job
        assert "scripts/image-publication-identity.py verify" in job


def test_tested_main_publishes_immutable_multiarch_openshell_runtime() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    job = workflow.split("\n  openshell-runtime-image:\n", 1)[1].split(
        "\n  certifier-image:\n", 1
    )[0]
    # The BUILD is deliberately NOT gated on the correctness jobs any more.
    # Building is cheap and reversible; deploying is not, so the gate moved to
    # the `tested-` tag (see the test below). Asserting the old `needs:` list
    # here would re-couple the two the moment anyone made this test pass.
    assert "needs: [publication-scope]" in job
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
        "CLAUDE_VERSION=2.1.220",
        "CURSOR_VERSION=2026.07.23-e383d2b",
        "OPENCODE_VERSION=1.18.18",
        "PI_VERSION=0.84.2",
    ):
        assert version in job
        # Substring-anywhere is too weak on its own: the reviewed build_args in
        # scripts/image-publication-identity.py must ALSO be passed to the
        # `plan` step, or build_plan raises "image build arguments differ from
        # the reviewed contract" and the job fails on main. Adding
        # OPENCODE_VERSION to the buildx args and the identity spec while
        # missing the plan step did exactly that.
        assert "--build-arg %s" % version in job, (
            "%s is missing from the image-publication-identity plan step" % version
        )


def test_plan_build_args_match_the_reviewed_identity_contract() -> None:
    """The plan step's --build-arg set must equal IMAGE_SPECS, exactly.

    build_plan compares the parsed args against the reviewed contract and
    raises "image build arguments differ from the reviewed contract" on ANY
    difference, so a version added in one place and not the other is a hard
    failure on push-to-main -- a branch where the job does not even run, so CI
    on the PR cannot catch it.

    That is not hypothetical: OPENCODE_VERSION was added to the buildx
    build-args, the asset-prep env and IMAGE_SPECS while the plan step was
    missed, and every existing assertion still passed because they only
    checked that the string appeared SOMEWHERE in the job.
    """
    import importlib.util
    import re

    spec = importlib.util.spec_from_file_location(
        "image_publication_identity", ROOT / "scripts" / "image-publication-identity.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    reviewed = dict(module.IMAGE_SPECS["openshell-runtime"]["build_args"])

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("\n  openshell-runtime-image:\n", 1)[1].split(
        "\n  certifier-image:\n", 1
    )[0]
    plan_step = job.split("- id: image-reuse", 1)[0]
    passed = dict(re.findall(r"--build-arg ([A-Z_]+)=(\S+)", plan_step))

    assert passed == reviewed, (
        "plan step build-args and IMAGE_SPECS disagree; symmetric difference: %s"
        % sorted(set(passed.items()) ^ set(reviewed.items()))
    )


def test_mainline_uses_fail_closed_impact_selection_between_nightly_full_runs() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    mainline = _workflow_job(workflow, "mainline")
    nightly = _workflow_job(workflow, "nightly")

    assert "fetch-depth: 0" in mainline
    assert "MAC_TEST_SELECT_BASE: ${{ github.event.before }}" in mainline
    assert "scripts/run-contract-tests.sh" in mainline
    assert "MAC_TEST_SELECT_BASE" not in nightly
    assert "scripts/run-contract-tests.sh" in nightly


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
    # What must stay blocked on the live Postgres contract is the DEPLOYABLE
    # image, not the build. Both certifier publication and the `tested-` tag
    # name postgres-contract in their needs; the runtime build no longer does.
    tested = workflow.split("\n  openshell-runtime-tested:\n", 1)[1].split(
        "\n  report-main-red:\n", 1
    )[0]
    assert "postgres-contract" in tested
    assert (
        workflow.count(
            "needs: [dead-code, mainline, compatibility, postgres-contract, publication-scope]"
        )
        == 2
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
    assert "scripts/image-publication-identity.py verify" in job
    assert "--plan mac-image-publication/plan.json" in job
    verifier = (ROOT / "scripts" / "image-publication-identity.py").read_text(
        encoding="utf-8"
    )
    assert '"--network"' in verifier
    assert '"/bin/sh"' in verifier
    assert 'test "$(id -u)" = 10001' in verifier
    assert "test -x /opt/mac-venv/bin/mac-git-askpass" in verifier
    assert (
        "import cryptography, fastapi, kubernetes, mac.api, psycopg, uvicorn, yaml"
        in verifier
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
