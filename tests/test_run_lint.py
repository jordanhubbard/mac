"""make lint and make lint-fix are a diagnose/apply pair, including format.

A gate that does not check format cannot warn before lint-fix rewrites the
tree. That failure already happened: lint was green, lint-fix reformatted
hundreds of files.
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run-lint.sh"


def _case_branch(text: str, label: str, next_label: str) -> str:
    start = text.index(label)
    end = text.index(next_label, start + 1)
    return text[start:end]


def test_lint_script_exists():
    assert SCRIPT.is_file()


def test_lint_reports_format_drift():
    """The no-arg path is make lint. It must run the same format checker as --format-check."""
    branch = _case_branch(SCRIPT.read_text(encoding="utf-8"), '    "")', "    *)")
    assert "ruff check ." in branch
    assert "ruff format --check" in branch


def test_lint_fix_applies_the_same_tools():
    branch = _case_branch(
        SCRIPT.read_text(encoding="utf-8"),
        "    --fix)",
        "    --format-check)",
    )
    assert "ruff check --fix" in branch
    assert "ruff format ." in branch
