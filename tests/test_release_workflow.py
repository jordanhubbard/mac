"""The single-command release workflow must retain its safe ordering."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release.sh"


def test_release_workflow_requires_gates_then_pr_tag_artifact_and_optional_rollout():
    text = SCRIPT.read_text(encoding="utf-8")
    required = [
        "make lint",
        "make test",
        "make docs-check",
        'git switch -c "$branch"',
        'gh pr checks "$pr_url" --watch --fail-fast',
        'gh pr merge "$pr_url" --squash --delete-branch',
        "git pull --ff-only origin main",
        'git tag -a "$tag"',
        'git push origin "$tag"',
        "gh run watch",
        'make deploy HUB="$fleet"',
    ]
    missing = [item for item in required if item not in text]
    assert not missing, missing
    assert (
        text.index('gh pr merge "$pr_url"')
        < text.index('git tag -a "$tag"')
        < text.index('make deploy HUB="$fleet"')
    )


def test_makefile_exposes_release_target():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "release: ## Create a tagged GitHub release" in text
    assert "scripts/release.sh $(BUMP)" in text
