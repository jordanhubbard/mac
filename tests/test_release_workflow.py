"""The single-command release workflow must retain its safe ordering."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "release.sh"


def test_release_workflow_requires_gates_then_pr_tag_and_artifact():
    text = SCRIPT.read_text(encoding="utf-8")
    required = [
        "make lint",
        "make test",
        "make docs-check",
        "--docs-dir is required for every release",
        "scripts/generate-docs-reference.py --write",
        'python3 "$docs_dir/build_deck.py"',
        'git switch -c "$branch"',
        'gh pr checks "$pr_url" --watch --fail-fast',
        'gh pr merge "$pr_url" --squash --delete-branch',
        "git pull --ff-only origin main",
        'git tag -a "$tag"',
        'git push origin "$tag"',
        "gh run watch",
    ]
    missing = [item for item in required if item not in text]
    assert not missing, missing
    assert text.index('gh pr merge "$pr_url"') < text.index('git tag -a "$tag"')
    # Rollout is scripts/fleet-update's job, not the release's.
    assert "make deploy" not in text


def test_makefile_exposes_release_target():
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "release: ## Create a tagged GitHub release" in text
    assert "RELEASE_DOCS=docs/presentation" in text
    assert 'scripts/release.sh $(BUMP) --docs-dir "$(RELEASE_DOCS)"' in text
