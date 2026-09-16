"""Portfolio CI must not fail main by pushing to a protected branch.

The job used to ``git push origin HEAD:main`` after rebuilding
``src/mac/data/test_impact_map.json``. Branch protection returns GH013, the
job went red, and a red main meant "git was not allowed to push" rather than
"the product is red". Landing the refresh is a pull request; a blocked push
must not fail the job. The map is already uploaded as an artifact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def portfolio_job(workflow):
    return workflow["jobs"]["portfolio"]


def test_portfolio_job_exists(workflow):
    assert "portfolio" in workflow["jobs"]


def _map_refresh_script(portfolio_job) -> str:
    for step in portfolio_job["steps"]:
        run = str(step.get("run") or "")
        if "gh pr create" in run or "impact-map-refresh" in run:
            return run
    raise AssertionError("portfolio job has no impact-map refresh step")


def test_portfolio_does_not_push_to_protected_main(portfolio_job):
    script = _map_refresh_script(portfolio_job)

    assert "HEAD:main" not in script
    assert "git push origin HEAD:main" not in script
    assert "refs/heads/main" not in script or "github.ref" in script


def test_portfolio_lands_the_map_via_a_pull_request(portfolio_job):
    script = _map_refresh_script(portfolio_job)

    assert "gh pr create" in script
    assert "ci/impact-map-refresh" in script
    assert portfolio_job["permissions"].get("contents") == "write"
    assert portfolio_job["permissions"].get("pull-requests") == "write"


def test_a_blocked_map_push_does_not_fail_the_job(portfolio_job):
    """The artifact is already uploaded. GH013 must not paint main red."""
    script = _map_refresh_script(portfolio_job)

    assert "exit 1" not in script
    assert "could not push" in script
    assert "test-portfolio" in script


def test_report_main_red_watches_portfolio_now_that_it_cannot_false_red(workflow):
    watched = set(workflow["jobs"]["report-main-red"]["needs"])

    assert "portfolio" in watched
