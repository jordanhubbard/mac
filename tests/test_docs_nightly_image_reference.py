"""The nightly documentation boundary must name an image that actually exists.

The Documentation workflow's `live-boundaries` job proves the documented ARM64
boundary by running the published main image. It used to name

    ghcr.io/jordanhubbard/mac:git-${GITHUB_SHA}

which looks like the obvious identity for "the image built from this commit" and
is not one. CI publishes that tag only from the step that *builds*, and that step
is skipped whenever the frozen image inputs are unchanged and the previously
published digest is reused. A tip commit that touched only documentation, tests
or workflows therefore has a green CI run, a perfectly good image, and no
`git-<sha>` tag at all -- so the nightly failed with "manifest unknown" and
reported a broken ARM64 boundary that was never broken. Observed on
d623f3d (Documentation run 32334374935): CI passed at 04:53, the nightly failed
at 05:08, and the package had no `git-d623f3d...` version.

The content-addressed `inputs-<sha256>` tag is the identity CI publishes under
when it builds and reuses when it does not, so it resolves for every commit.
Both halves are asserted here: that the nightly consumes the content tag, and
that the `git-<sha>` tag really is conditional -- if publication ever becomes
unconditional, this test is where that assumption is recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCS_WORKFLOW = REPO_ROOT / ".github/workflows/docs.yml"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"
MAC_IMAGE = "ghcr.io/jordanhubbard/mac"
ARM64_STEP = "Execute the immutable main image on ARM64"
RESOLVE_STEP = "Resolve the published identity of the main image"
PUBLISH_STEP = "Publish multi-platform MAC deployment image"


def _steps(workflow: Path, job: str) -> list[dict]:
    jobs = yaml.safe_load(workflow.read_text(encoding="utf-8"))["jobs"]
    return jobs[job]["steps"]


def _step(steps: list[dict], name: str) -> dict:
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"no step named {name!r} in {[s.get('name') for s in steps]}")


@pytest.fixture(scope="module")
def nightly():
    return _steps(DOCS_WORKFLOW, "live-boundaries")


@pytest.fixture(scope="module")
def deployment_image():
    return _steps(CI_WORKFLOW, "docker")


def test_the_arm64_boundary_does_not_name_a_conditionally_published_tag(nightly):
    """The defect: a tag that exists only when the image was rebuilt."""
    run = _step(nightly, ARM64_STEP)["run"]

    assert f"{MAC_IMAGE}:git-" not in run, (
        "the nightly names a git-<sha> tag, which is absent for any commit whose "
        "image was reused rather than rebuilt"
    )


def test_the_arm64_boundary_runs_the_content_addressed_identity(nightly):
    resolve = _step(nightly, RESOLVE_STEP)
    assert resolve.get("id") == "image"
    assert "scripts/image-publication-identity.py plan" in resolve["run"]
    assert "--kind mac" in resolve["run"]
    assert '--requested-revision "$GITHUB_SHA"' in resolve["run"]

    execute = _step(nightly, ARM64_STEP)
    assert execute["env"]["MAC_IMAGE"] == "${{ steps.image.outputs.content_tag }}"
    assert '"$MAC_IMAGE"' in execute["run"]


def test_the_documented_arm64_boundary_is_still_exercised(nightly):
    """Resolving the reference differently must not weaken what it proves."""
    run = _step(nightly, ARM64_STEP)["run"]

    assert "--platform linux/arm64" in run
    assert "--entrypoint /opt/mac-venv/bin/mac" in run


def test_the_git_tag_is_published_only_when_the_image_is_rebuilt(deployment_image):
    """The reason the nightly cannot trust `git-<sha>`."""
    publish = _step(deployment_image, PUBLISH_STEP)

    assert publish["if"] == "steps.image-reuse-eligibility.outputs.reuse != 'true'"
    assert f"{MAC_IMAGE}:git-" in publish["with"]["tags"]


def test_the_content_tag_is_published_by_the_same_step(deployment_image):
    """And the reason it can trust the content tag: CI pushes it on every build,
    and reuse is only qualified against an already-published one."""
    publish = _step(deployment_image, PUBLISH_STEP)

    assert "steps.image-plan.outputs.content_tag" in publish["with"]["tags"]
