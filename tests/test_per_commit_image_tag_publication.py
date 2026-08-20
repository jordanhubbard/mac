"""Every commit CI publishes an image for must get a `git-<sha>` tag.

The nightly Documentation workflow and `mac.worker` both address a published
image by commit: `ghcr.io/<repo>:git-<40-hex>`. That tag used to be produced by
exactly one step -- the multi-platform build-and-push -- which is skipped
whenever the frozen image inputs are unchanged and the previously published
digest is reused. A reusing commit therefore published no per-commit tag at
all, and its readers failed with "manifest unknown" against an image that was
fine (Documentation run 32334374935 on d623f3d).

These tests lock the repaired invariant from both ends: every disposition the
publication jobs can reach publishes the tag, and the two readers still ask for
the shape that is published.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List

import yaml


ROOT = Path(__file__).resolve().parents[1]

#: Publication job -> the GHCR repository whose per-commit tag it owns.
PER_COMMIT_TAG_PUBLISHERS = {
    "docker": "ghcr.io/jordanhubbard/mac",
    "openshell-runtime-image": "ghcr.io/jordanhubbard/mac-openshell-runtime",
}

REUSE_CONDITION = "steps.image-identity.outputs.disposition == 'reused'"
BUILD_CONDITION = "steps.image-reuse-eligibility.outputs.reuse != 'true'"


def _ci_workflow() -> Dict[str, Any]:
    return yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))


def _steps(job_name: str) -> List[Dict[str, Any]]:
    return list(_ci_workflow()["jobs"][job_name]["steps"])


def _index_of(steps: List[Dict[str, Any]], predicate) -> int:
    matches = [index for index, step in enumerate(steps) if predicate(step)]
    assert len(matches) == 1, "expected exactly one matching step, found %d" % len(matches)
    return matches[0]


def test_build_path_publishes_the_per_commit_tag_only_when_it_builds() -> None:
    """The original producer, and the gate that makes a second one necessary."""

    for job_name, repository in PER_COMMIT_TAG_PUBLISHERS.items():
        steps = _steps(job_name)
        tag = "%s:git-${{ github.sha }}" % repository
        publish = steps[_index_of(steps, lambda step: tag in str(step.get("with", {}).get("tags")))]

        assert publish.get("id") == "publish"
        assert publish["with"]["push"] is True
        assert "linux/arm64" in publish["with"]["platforms"], (
            "%s must stay multi-platform: the nightly Documentation boundary "
            "executes it under --platform linux/arm64" % repository
        )
        # If this ever becomes unconditional the reuse retag below is
        # redundant rather than wrong -- but while it is gated, the retag is
        # the only thing standing between a reusing commit and a tag that
        # never existed.
        assert publish["if"] == BUILD_CONDITION


def test_reuse_path_republishes_the_per_commit_tag_by_digest() -> None:
    for job_name, repository in PER_COMMIT_TAG_PUBLISHERS.items():
        steps = _steps(job_name)
        retag = steps[
            _index_of(
                steps,
                lambda step: "imagetools create" in str(step.get("run", "")),
            )
        ]

        assert retag["if"] == REUSE_CONDITION, (
            "%s loses its per-commit tag on any commit that reuses a digest "
            "unless this step runs for every reused disposition" % repository
        )
        run = retag["run"]
        assert "repo=%s\n" % repository in run
        assert '--tag "$repo:git-${GITHUB_SHA}"' in run
        # By digest, never by tag: the reused content is identified by the
        # digest this job already qualified, so the new tag cannot drift onto
        # different content.
        assert '"$repo@$IMAGE_DIGEST"' in run
        assert "^sha256:[0-9a-f]{64}$" in run
        assert retag["env"]["IMAGE_DIGEST"] == "${{ steps.image-identity.outputs.digest }}"


def test_every_disposition_the_publication_jobs_reach_publishes_the_tag() -> None:
    """`built` publishes it inline; `reused` retags. There is no third case."""

    for job_name in PER_COMMIT_TAG_PUBLISHERS:
        steps = _steps(job_name)
        identity = steps[_index_of(steps, lambda step: step.get("id") == "image-identity")]
        dispositions = set(re.findall(r"disposition=(\w+)", identity["run"]))

        assert dispositions == {"built", "reused"}
        assert 'echo "disposition=$disposition" >> "$GITHUB_OUTPUT"' in identity["run"]


def test_the_reuse_retag_runs_where_it_can_actually_reach_the_registry() -> None:
    """Ordering and tooling, not just presence.

    `imagetools create` pushes, so it needs the GHCR login and a buildx
    builder, and it needs the digest the identity step resolved. Buildx is set
    up unconditionally in both jobs today; if that were ever gated on the build
    path the retag would fail on exactly the commits it exists for.
    """

    for job_name in PER_COMMIT_TAG_PUBLISHERS:
        steps = _steps(job_name)
        login = _index_of(
            steps, lambda step: str(step.get("uses", "")).startswith("docker/login-action@")
        )
        buildx = _index_of(
            steps,
            lambda step: str(step.get("uses", "")).startswith("docker/setup-buildx-action@"),
        )
        identity = _index_of(steps, lambda step: step.get("id") == "image-identity")
        retag = _index_of(steps, lambda step: "imagetools create" in str(step.get("run", "")))

        assert login < retag
        assert buildx < retag
        assert identity < retag
        assert "if" not in steps[buildx]


def test_the_readers_still_ask_for_the_tag_that_is_published() -> None:
    """The invariant is only worth locking while something depends on it."""

    docs = yaml.safe_load((ROOT / ".github" / "workflows" / "docs.yml").read_text("utf-8"))
    nightly = docs["jobs"]["live-boundaries"]
    arm64 = [
        step
        for step in nightly["steps"]
        if "ghcr.io/jordanhubbard/mac:git-${GITHUB_SHA}" in str(step.get("run", ""))
    ]
    assert len(arm64) == 1
    assert "--platform linux/arm64" in arm64[0]["run"]

    worker = (ROOT / "src" / "mac" / "worker.py").read_text("utf-8")
    assert '_OPENSHELL_RUNTIME_REPO = "ghcr.io/jordanhubbard/mac-openshell-runtime"' in worker
    assert 'tag = "%s:git-%s" % (_OPENSHELL_RUNTIME_REPO, source_sha)' in worker
