"""A red main must reach a person, not just a red square.

main CI failed on 28 of 30 runs between 2026-08-02 and 2026-08-06 and nobody
noticed for four days. The jobs that failed -- ``docker`` and the two publish
steps -- are SKIPPED on pull requests, so every PR in that window showed green
and merged honestly while the post-merge run went red.

The failing check was the black-box "deployed hub authentication and task round
trip", which is precisely the one that would catch a bad deploy, and it was
dark through a week in which all eight fleet hosts were deployed from that
main.

#285 fixed the failure. It did not fix the silence, which is the part that let
one broken commit become four broken days. ``report-main-red`` is the consumer
that was missing.

These tests guard the two ways it would quietly stop working: someone deletes
it, or a job is added to CI whose failure it does not watch.
"""

from __future__ import annotations

from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"

#: Jobs whose failure must reach a person. These are the ones that do NOT run
#: on pull requests, so a failure here is invisible until something says so.
#: If a job like this is added to CI, add it here too.
MAIN_ONLY_CRITICAL = {"docker", "mainline", "publication-scope", "portfolio"}


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(CI.read_text(encoding="utf-8"))


def test_a_job_exists_to_surface_a_red_main(workflow):
    assert "report-main-red" in workflow["jobs"], (
        "nothing surfaces a red main. Between 2026-08-02 and 2026-08-06 that "
        "cost four days of a broken deploy check that nobody saw."
    )


def test_it_fires_only_on_a_failed_push_to_main(workflow):
    """It must not fire on pull requests, or it becomes noise and gets muted."""
    condition = workflow["jobs"]["report-main-red"]["if"]

    assert "failure()" in condition
    assert "github.event_name == 'push'" in condition
    assert "refs/heads/main" in condition
    # always() is required or the job is skipped when a dependency fails --
    # which is exactly and only when it needs to run.
    assert "always()" in condition, (
        "without always() the job is skipped when a needed job fails, so it "
        "would never run in the one case it exists for"
    )


def test_it_watches_every_job_that_pull_requests_never_run(workflow):
    """A failure it does not depend on is a failure it cannot report."""
    watched = set(workflow["jobs"]["report-main-red"]["needs"])
    missing = MAIN_ONLY_CRITICAL - watched

    assert not missing, (
        "report-main-red does not depend on %s, so a failure there is invisible "
        "again -- the exact condition this job exists for" % sorted(missing)
    )


def test_every_watched_job_actually_exists(workflow):
    """A typo in `needs` makes the job silently never run."""
    jobs = set(workflow["jobs"])
    watched = set(workflow["jobs"]["report-main-red"]["needs"])
    unknown = watched - jobs

    assert not unknown, "report-main-red depends on non-existent job(s): %s" % sorted(unknown)


def test_it_can_actually_write_an_issue(workflow):
    """Without issues:write the job runs and fails to report, which is worse
    than not running: it looks like coverage and is not."""
    permissions = workflow["jobs"]["report-main-red"].get("permissions") or {}

    assert permissions.get("issues") == "write", (
        "the job cannot open an issue, so it would fail silently and the red "
        "main would stay invisible"
    )


def test_it_reuses_one_issue_rather_than_opening_many(workflow):
    """A new issue per failing commit is its own kind of noise, and noise is
    what stops people reading."""
    steps = workflow["jobs"]["report-main-red"]["steps"]
    script = " ".join(str(s.get("run") or "") for s in steps)

    assert "gh issue list" in script, "does not look for an existing issue"
    assert "gh issue comment" in script, "cannot update an existing issue"
    assert "gh issue create" in script, "cannot open the first issue"
