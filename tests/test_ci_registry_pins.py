"""CI preloads BuildKit by digest and uses a pinned relay collector mirror."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SETUP_BUILDX = "docker/setup-buildx-action@"
#: Docker's registry hosts. A reference to one of these without a login is the
#: anonymous pull that hit the rate limit.
DOCKER_HUB_HOSTS = ("registry-1.docker.io", "index.docker.io", "docker.io")
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


def test_every_setup_buildx_step_uses_the_acquired_buildkit_image() -> None:
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
                assert driver_opts == "image=${{ steps.buildkit-image.outputs.image }}"
                preceding = job["steps"][: job["steps"].index(step)]
                assert any(
                    candidate.get("id") == "buildkit-image"
                    and candidate.get("run") == "scripts/acquire-ci-buildkit.sh"
                    for candidate in preceding
                ), where
    assert buildx_steps == 5, (
        f"expected the 5 known setup-buildx steps, found {buildx_steps}; "
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


@pytest.mark.parametrize("upstream_available", [True, False])
def test_buildkit_registry_fallback_keeps_the_pin(tmp_path, upstream_available):
    docker = tmp_path / "docker"
    docker.write_text(
        "#!/bin/sh\n"
        'printf "%s\\n" "$*" >> "$PULL_LOG"\n'
        'case "$*" in "pull docker.io/moby/buildkit@"*) exit "$UPSTREAM_STATUS";; esac\n'
        "exit 1\n"
    )
    docker.chmod(0o700)
    output = tmp_path / "output"
    pulls = tmp_path / "pulls"
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "acquire-ci-buildkit.sh")],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "GITHUB_OUTPUT": str(output),
            "PULL_LOG": str(pulls),
            "UPSTREAM_STATUS": "0" if upstream_available else "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    attempted = [
        line.removeprefix("pull ")
        for line in pulls.read_text().splitlines()
        if line.startswith("pull ")
    ]
    assert len(attempted) == 2
    mirror, upstream = attempted
    assert mirror.startswith("public.ecr.aws/vend/moby/buildkit@")
    assert upstream.startswith("docker.io/moby/buildkit@")
    assert mirror.split("@")[1] == upstream.split("@")[1]
    assert DIGEST.search(upstream)
    if upstream_available:
        assert result.returncode == 0, result.stderr
        assert output.read_text() == f"image={upstream}\n"
    else:
        assert result.returncode == 1
        assert not output.exists()
