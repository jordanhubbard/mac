from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
HARNESS = ROOT / "scripts" / "test-docs.py"


def _module():
    spec = importlib.util.spec_from_file_location("mac_test_docs", HARNESS)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_book_has_ordered_chapters_and_executable_shell_contracts():
    chapters = _module().load_chapters()
    assert [chapter.number for chapter in chapters] == list(range(1, 19))
    assert all(chapter.blocks for chapter in chapters)
    assert all(
        block.language in {"bash", "sh", "shell"}
        for chapter in chapters
        for block in chapter.blocks
    )


def test_non_book_pages_cannot_bypass_the_shell_execution_contract():
    module = _module()
    module.load_chapters()


def test_all_book_shell_examples_execute_hermetically():
    result = subprocess.run(
        [sys.executable, str(HARNESS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=360,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert '"chapter_count": 18' in result.stdout


def test_docs_repository_fixture_has_reachable_secret_free_origin(tmp_path):
    module = _module()
    module._prepare_repository_fixture(tmp_path)
    repository = tmp_path / "sample-repo"
    expected_remote = (tmp_path / "sample-origin.git").resolve()

    origin = subprocess.run(
        ["git", "-C", str(repository), "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert origin.returncode == 0, origin.stderr
    assert Path(origin.stdout.strip()).resolve() == expected_remote
    probe = subprocess.run(
        ["git", "-C", str(repository), "ls-remote", "--heads", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def test_generated_documentation_reference_is_current():
    result = subprocess.run(
        [sys.executable, "scripts/generate-docs-reference.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_generated_cli_reference_covers_every_top_level_command():
    module_path = ROOT / "scripts" / "generate-docs-reference.py"
    spec = importlib.util.spec_from_file_location("mac_generate_docs_reference", module_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root_help = module._help()
    reference = module.cli_reference()
    for command in module._top_level_commands(root_help):
        assert f"## mac {command}\n" in reference


def test_documentation_ci_covers_platforms_live_boundaries_and_versioning():
    workflow = (ROOT / ".github" / "workflows" / "docs.yml").read_text(encoding="utf-8")
    for required in (
        "ubuntu-latest",
        "macos-14",
        "tests/test_postgres_live.py",
        "helm/kind-action@",
        "--platform linux/arm64",
        "mike deploy --push --update-aliases dev",
        'mike deploy --push --update-aliases "$version" latest',
    ):
        assert required in workflow
