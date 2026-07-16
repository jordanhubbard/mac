"""Tests for scripts/select-sanity-tests.py fail-closed sanity selection."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "select-sanity-tests.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("mac_select_sanity_tests", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def selector():
    return _load_module()


def test_empty_scope_is_fail_closed_full(selector):
    result = selector.select([])
    assert result["schema"] == selector.SCHEMA
    assert result["mode"] == "full"
    assert result["reason"] == "no_trustworthy_changed_file_scope"
    assert result["changed_files"] == []
    assert result["tests"] == []


def test_broad_path_forces_full_scope(selector):
    result = selector.select(["pyproject.toml", "src/mac/services.py"])
    assert result["mode"] == "full"
    assert result["reason"] == "test_or_shared_runtime_infrastructure_changed"
    assert "pyproject.toml" in result["broad_files"]
    assert result["tests"] == []


def test_broad_prefix_forces_full_scope(selector):
    result = selector.select([".github/workflows/ci.yml"])
    assert result["mode"] == "full"
    assert result["broad_files"] == [".github/workflows/ci.yml"]


def test_non_code_change_is_focused_with_direct_tests(selector, monkeypatch):
    # A changed test file exists on disk -> included; a doc file drives the
    # non_code_change branch because there is no src/plugin code change.
    monkeypatch.setattr(selector.Path, "is_file", lambda self: True)
    result = selector.select(["docs/readme.md", "tests/test_example.py"])
    assert result["mode"] == "focused"
    assert result["reason"] == "non_code_change"
    assert result["tests"] == ["tests/test_example.py"]


def test_directly_changed_test_requires_file_on_disk(selector):
    # A test path that does not exist on disk must not be selected.
    result = selector.select(["tests/test_does_not_exist_zzz.py"])
    assert result["mode"] == "focused"
    assert result["reason"] == "non_code_change"
    assert result["tests"] == []


def test_module_test_candidates_maps_src_to_tests(selector):
    candidates = selector._module_test_candidates("src/mac/select_sanity_tests.py")
    # There is no tests/test_select_sanity_tests.py sibling pattern guaranteed;
    # candidates are only paths that exist, so the list is a subset of tests/.
    for candidate in candidates:
        assert candidate.startswith("tests/")
        assert (ROOT / candidate).is_file()


def test_module_test_candidates_ignores_non_src(selector):
    assert selector._module_test_candidates("plugin/foo.py") == []
    assert selector._module_test_candidates("src/mac/data.txt") == []


def test_code_change_without_mapped_tests_is_full(selector, monkeypatch):
    monkeypatch.setattr(selector, "_module_test_candidates", lambda path: [])
    monkeypatch.setattr(selector, "_codegraph_affected", lambda changed: ([], None))
    result = selector.select(["src/mac/brand_new_module.py"])
    assert result["mode"] == "full"
    assert result["reason"] == "code_change_has_no_reliable_affected_tests"
    assert result["tests"] == []


def test_code_change_uses_codegraph_problem_reason(selector, monkeypatch):
    monkeypatch.setattr(selector, "_module_test_candidates", lambda path: [])
    monkeypatch.setattr(
        selector, "_codegraph_affected", lambda changed: ([], "codegraph_unavailable")
    )
    result = selector.select(["src/mac/brand_new_module.py"])
    assert result["mode"] == "full"
    assert result["reason"] == "codegraph_unavailable"


def test_code_change_focused_scope_includes_canaries(selector, monkeypatch):
    monkeypatch.setattr(
        selector, "_module_test_candidates", lambda path: ["tests/test_mapped.py"]
    )
    monkeypatch.setattr(selector, "_codegraph_affected", lambda changed: ([], None))
    monkeypatch.setattr(selector.Path, "is_file", lambda self: True)
    result = selector.select(["src/mac/thing.py"])
    assert result["mode"] == "focused"
    assert result["reason"] == "direct_codegraph_and_canary_scope"
    assert "tests/test_mapped.py" in result["tests"]
    for canary in selector.CANARIES:
        assert canary in result["tests"]
    assert result["tests"] == sorted(result["tests"])
    assert result["codegraph_problem"] is None


def test_focused_scope_propagates_codegraph_problem(selector, monkeypatch):
    # A degraded CodeGraph must not silently vanish: even when a focused scope
    # is still produced from module-mapped tests, the selector fails closed by
    # recording the CodeGraph failure in codegraph_problem for diagnostics.
    monkeypatch.setattr(
        selector, "_module_test_candidates", lambda path: ["tests/test_mapped.py"]
    )
    monkeypatch.setattr(
        selector,
        "_codegraph_affected",
        lambda changed: ([], "codegraph_affected_failed"),
    )
    monkeypatch.setattr(selector.Path, "is_file", lambda self: True)
    result = selector.select(["src/mac/thing.py"])
    assert result["mode"] == "focused"
    assert result["reason"] == "direct_codegraph_and_canary_scope"
    assert result["codegraph_problem"] == "codegraph_affected_failed"


def test_codegraph_affected_unavailable(selector, monkeypatch):
    monkeypatch.setattr(selector.shutil, "which", lambda name: None)
    tests, problem = selector._codegraph_affected(["src/mac/thing.py"])
    assert tests == []
    assert problem == "codegraph_unavailable"


def test_codegraph_affected_no_source_changes(selector, monkeypatch):
    monkeypatch.setattr(selector.shutil, "which", lambda name: "/usr/bin/codegraph")
    tests, problem = selector._codegraph_affected(["docs/readme.md"])
    assert tests == []
    assert problem is None


def _fake_run(returncode=0, stdout="", stderr=""):
    class _Result:
        pass

    result = _Result()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


def test_codegraph_affected_command_failure(selector, monkeypatch):
    monkeypatch.setattr(selector.shutil, "which", lambda name: "/usr/bin/codegraph")
    monkeypatch.setattr(
        selector.subprocess, "run", lambda *a, **k: _fake_run(returncode=2)
    )
    tests, problem = selector._codegraph_affected(["src/mac/thing.py"])
    assert tests == []
    assert problem == "codegraph_affected_failed"


def test_codegraph_affected_invalid_json(selector, monkeypatch):
    monkeypatch.setattr(selector.shutil, "which", lambda name: "/usr/bin/codegraph")
    monkeypatch.setattr(
        selector.subprocess, "run", lambda *a, **k: _fake_run(stdout="not-json")
    )
    tests, problem = selector._codegraph_affected(["src/mac/thing.py"])
    assert tests == []
    assert problem == "codegraph_affected_invalid_json"


def test_codegraph_affected_filters_to_existing_test_files(selector, monkeypatch):
    monkeypatch.setattr(selector.shutil, "which", lambda name: "/usr/bin/codegraph")
    payload = {
        "affectedTests": [
            "tests/test_missing_zzz.py",
            "src/mac/not_a_test.py",
            "tests/conftest.py",
        ]
    }
    monkeypatch.setattr(
        selector.subprocess,
        "run",
        lambda *a, **k: _fake_run(stdout=json.dumps(payload)),
    )
    monkeypatch.setattr(selector.Path, "is_file", lambda self: True)
    tests, problem = selector._codegraph_affected(["src/mac/thing.py"])
    assert problem is None
    assert "tests/test_missing_zzz.py" in tests
    assert "tests/conftest.py" in tests
    assert "src/mac/not_a_test.py" not in tests


def test_git_changed_files_with_base(selector, monkeypatch):
    calls = {}

    def _run(command, **kwargs):
        calls["command"] = command
        return _fake_run(stdout="b.py\na.py\n a.py \n\n")

    monkeypatch.setattr(selector.subprocess, "run", _run)
    files = selector._git_changed_files("origin/main")
    assert files == ["a.py", "b.py"]
    assert "origin/main...HEAD" in calls["command"]


def test_git_changed_files_without_base(selector, monkeypatch):
    calls = {}

    def _run(command, **kwargs):
        calls["command"] = command
        return _fake_run(stdout="x.py\n")

    monkeypatch.setattr(selector.subprocess, "run", _run)
    files = selector._git_changed_files(None)
    assert files == ["x.py"]
    assert calls["command"] == ["git", "diff", "--name-only", "HEAD"]


def test_git_changed_files_raises_on_failure(selector, monkeypatch):
    monkeypatch.setattr(
        selector.subprocess, "run", lambda *a, **k: _fake_run(returncode=1, stderr="boom")
    )
    with pytest.raises(RuntimeError, match="boom"):
        selector._git_changed_files(None)


def test_main_json_output(selector, capsys):
    rc = selector.main(["--changed-file", "README.md"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == selector.SCHEMA
    assert document["changed_files"] == ["README.md"]


def test_main_tests_only_output(selector, monkeypatch, capsys):
    monkeypatch.setattr(
        selector, "select", lambda changed: {"tests": ["tests/test_a.py", "tests/test_b.py"]}
    )
    rc = selector.main(["--changed-file", "src/mac/thing.py", "--tests-only"])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert out_lines == ["tests/test_a.py", "tests/test_b.py"]


def test_main_handles_selection_error(selector, monkeypatch, capsys):
    def _boom(base):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(selector, "_git_changed_files", _boom)
    rc = selector.main([])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["mode"] == "full"
    assert document["reason"] == "selection_error"
    assert "git exploded" in document["error"]


# --- Explicit task-contract scenario coverage (named examples) ---


def test_makefile_broad_path_forces_full(selector):
    # Scenario (2): Makefile is an explicit broad path.
    result = selector.select(["Makefile"])
    assert result["mode"] == "full"
    assert result["reason"] == "test_or_shared_runtime_infrastructure_changed"
    assert "Makefile" in result["broad_files"]
    assert result["tests"] == []


def test_scripts_prefix_broad_path_forces_full(selector):
    # Scenario (3): any scripts/ prefixed path is broad -> full.
    result = selector.select(["scripts/foo.py"])
    assert result["mode"] == "full"
    assert result["reason"] == "test_or_shared_runtime_infrastructure_changed"
    assert "scripts/foo.py" in result["broad_files"]


def test_src_change_maps_to_matching_test(selector):
    # Scenario (5): a src/mac/<stem>.py change with a real matching
    # tests/test_<stem>.py sibling yields a focused scope listing that test.
    result = selector.select(["src/mac/agent_command.py"])
    assert result["mode"] == "focused"
    assert "tests/test_agent_command.py" in result["tests"]


def test_cli_changed_file_feeds_specific_files(selector, capsys):
    # Scenario (8): --changed-file supplies the exact selection input.
    rc = selector.main(["--changed-file", "docs/readme.md"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["changed_files"] == ["docs/readme.md"]
    assert document["mode"] == "focused"
    assert document["reason"] == "non_code_change"


def test_cli_tests_only_prints_one_path_per_line(selector, capsys):
    # Scenario (7): --tests-only emits one selected test path per line.
    rc = selector.main(["--changed-file", "src/mac/agent_command.py", "--tests-only"])
    assert rc == 0
    out_lines = capsys.readouterr().out.splitlines()
    assert "tests/test_agent_command.py" in out_lines
    assert all(line.strip() == line and line for line in out_lines)
