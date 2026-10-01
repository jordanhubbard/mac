"""Building the image and certifying it are different concerns.

The image publish used to carry

    needs: [dead-code, mainline, compatibility, postgres-contract,
            publication-scope]

so any red test anywhere stopped an image being produced -- and with no image,
deploy-mac-fleet.sh refuses, so the FLEET became undeployable. That happened
three times in one night; the last was two stale assertions about a prompt
string, while seven nodes sat under dispatch hold waiting for the repair that
could not be delivered.

Building is cheap and reversible. Deploying is not. So the build waits only on
what decides whether the image may be published, and the correctness gates
certify the image instead, through a `tested-` tag on the same digest.

(The deploy script that refused an untagged image has since been deleted;
scripts/fleet-update deploys from a git checkout, not an image.)
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"

CORRECTNESS_GATES = {"dead-code", "mainline", "compatibility", "postgres-contract"}


@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]


def test_the_build_does_not_wait_on_unrelated_correctness_gates(jobs):
    """The defect: one red unit test made the fleet undeployable."""
    needs = set(jobs["openshell-runtime-image"]["needs"])

    assert not (needs & CORRECTNESS_GATES), (
        "publishing waits on %s; a failure there blocks image production and "
        "therefore every fleet deploy" % sorted(needs & CORRECTNESS_GATES)
    )


def test_the_build_still_waits_on_the_publication_scope_gate(jobs):
    """Decoupling is not the same as ungating. What may be published is still
    decided before anything is."""
    assert "publication-scope" in jobs["openshell-runtime-image"]["needs"]


def test_the_correctness_gates_moved_to_the_tested_marker(jobs):
    """They did not disappear -- they now certify a built image."""
    needs = set(jobs["openshell-runtime-tested"]["needs"])

    assert CORRECTNESS_GATES <= needs
    assert "openshell-runtime-image" in needs


def test_the_tested_marker_retags_by_digest(jobs):
    """By digest, not by rebuilding: a tag that pointed at different content
    than the one that was verified would certify the wrong thing."""
    steps = jobs["openshell-runtime-tested"]["steps"]
    script = "\n".join(str(step.get("run", "")) for step in steps)

    assert "imagetools create" in script
    assert "@$DIGEST" in script or "@${DIGEST}" in script


def test_the_publish_job_exposes_what_the_marker_consumes(jobs):
    outputs = jobs["openshell-runtime-image"].get("outputs") or {}

    assert "digest" in outputs and "frozen_inputs_sha256" in outputs
