"""CI must not pull its buildkit or collector images from anonymous Docker Hub.

On 2026-10-09 the GitHub-hosted runner IPs hit Docker Hub's unauthenticated
pull rate limit, which failed every retry and blocked PR #966. Two pulls
remained after the PostgreSQL images moved to ECR Public:

* ``docker/setup-buildx-action`` pulled ``moby/buildkit:buildx-stable-1``.
* The container-contract relay collector pulled
  ``otel/opentelemetry-collector-contrib:0.100.0``.

Both now come from mirrors at a pinned digest. These contracts keep a future
edit from quietly reintroducing the anonymous pull, which only shows up as a
red job on a shared runner IP.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SETUP_BUILDX = "docker/setup-buildx-action@"
#: Docker's registry hosts. A reference to one of these without a login is the
#: anonymous pull that hit the rate limit.
DOCKER_HUB_HOSTS = ("registry-1.docker.io", "index.docker.io", "docker.io")
BUILDKIT_MIRROR = "public.ecr.aws/vend/moby/buildkit"
COLLECTOR_REPO = "otel/opentelemetry-collector-contrib"
COLLECTOR_MIRROR = (
    "ghcr.io/open-telemetry/opentelemetry-collector-releases/opentelemetry-collector-contrib"
)
DIGEST = re.compile(r"@sha256:[0-9a-f]{64}\b")


def _workflow_paths() -> list[Path]:
    return sorted((ROOT / ".github" / "workflows").glob("*.yml"))


def _e2e_fixture_paths() -> list[Path]:
    return sorted(p for p in (ROOT / "tests" / "e2e").iterdir() if p.is_file())


def _ci_surface_text() -> dict[Path, str]:
    paths = [*_workflow_paths(), *_e2e_fixture_paths()]
    return {path: path.read_text(encoding="utf-8") for path in paths}


def test_ci_surface_does_not_reference_docker_hub() -> None:
    for path, text in _ci_surface_text().items():
        for host in DOCKER_HUB_HOSTS:
            assert host not in text, (
                f"{path.relative_to(ROOT)} references {host}: CI jobs must pull "
                "from a mirror, not anonymous Docker Hub"
            )


def test_every_setup_buildx_step_pins_the_buildkit_mirror_by_digest() -> None:
    yaml = pytest.importorskip("yaml")
    buildx_steps = 0
    for path in _workflow_paths():
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job_name, job in (workflow.get("jobs") or {}).items():
            for step in job.get("steps") or []:
                if SETUP_BUILDX not in str(step.get("uses", "")):
                    continue
                buildx_steps += 1
                where = f"{path.name}:{job_name}"
                # A docker-container buildx builder runs buildkit as its own
                # image; without driver-opts it is Docker Hub's
                # moby/buildkit:buildx-stable-1.
                driver_opts = str((step.get("with") or {}).get("driver-opts", ""))
                image_opts = [
                    part[len("image=") :]
                    for part in driver_opts.replace("\n", " ").split()
                    if part.startswith("image=")
                ]
                assert image_opts, (
                    f"{where}: setup-buildx has no image= driver-opt, so it pulls "
                    "moby/buildkit from anonymous Docker Hub"
                )
                image = image_opts[0]
                assert image.startswith(BUILDKIT_MIRROR), (
                    f"{where}: buildkit image must come from {BUILDKIT_MIRROR}, got {image}"
                )
                assert DIGEST.search(image), (
                    f"{where}: buildkit image is not pinned by digest: {image}"
                )
    assert buildx_steps == 4, (
        f"expected the 4 known setup-buildx steps, found {buildx_steps}; "
        "a new step must be pinned too"
    )


def test_relay_collector_pulls_the_collector_from_a_pinned_ghcr_mirror() -> None:
    yaml = pytest.importorskip("yaml")
    compose = yaml.safe_load(
        (ROOT / "tests" / "e2e" / "docker-compose.e2e.yaml").read_text(encoding="utf-8")
    )
    image = compose["services"]["relay-collector"]["image"]
    assert not image.startswith(COLLECTOR_REPO), (
        "the relay collector still pulls from anonymous Docker Hub"
    )
    assert image.startswith(COLLECTOR_MIRROR), (
        f"the relay collector image must come from {COLLECTOR_MIRROR}, got {image}"
    )
    assert DIGEST.search(image), f"the relay collector image is not pinned by digest: {image}"
