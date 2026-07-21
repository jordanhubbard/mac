"""Safety-matrix unit tests for scripts/resolve-impacted-tests.py.

Every path is driven through the pure ``resolve()`` function so no git or real
coverage run is required. The bar is fail-closed: whenever a changed file cannot
be safely mapped, the resolver must escalate to a full run.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "scripts" / "resolve-impacted-tests.py"
    name = "mac_resolve_impacted_tests"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass string annotations resolve.
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


R = _load()
BASE_SHA = "a" * 40


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    for rel in (
        "tests/test_foo.py",
        "tests/test_bar.py",
        "tests/test_always.py",
        "tests/test_canary.py",
        "tests/test_changed.py",
        "tests/test_env_config.py",
        "tests/test_resolve_impacted_tests.py",
    ):
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    return tmp_path


@pytest.fixture()
def impact_map() -> dict:
    return {
        "schema": "mac.test_impact_map.v1",
        "base_sha": BASE_SHA,
        "nodeids": [
            "tests/test_foo.py::test_a",
            "tests/test_foo.py::test_b",
            "tests/test_bar.py::test_c",
        ],
        "file_tests": {"src/mac/foo.py": [0, 1], "src/mac/bar.py": [2]},
        "file_line_tests": {
            "src/mac/foo.py": {"10": [0], "12": [0, 1]},
            "src/mac/bar.py": {"5": [2]},
        },
        "always_run": ["tests/test_always.py"],
    }


@pytest.fixture()
def policy() -> "R.SelectionPolicy":
    return R.SelectionPolicy(
        global_full_paths=frozenset({"test-policy.toml", "tests/conftest.py"}),
        always_run=("tests/test_canary.py",),
    )


def _resolve(repo, policy, impact_map, changed, base_lines=None, *, fresh=True, cg=(), cg_problem=None):
    # ``fresh`` models the IO-layer freshness decision: True => every mapped file
    # is trustworthy for this base (exact match), False => none (stale/divergent).
    fresh_files = list(impact_map.get("file_tests", {})) if (fresh and impact_map) else []
    return R.resolve(
        changed,
        base_lines or {},
        fresh_map_files=fresh_files,
        policy=policy,
        impact_map=impact_map,
        codegraph_tests=list(cg),
        codegraph_problem=cg_problem,
        repo_root=repo,
    )


def test_empty_changeset_is_full(repo, policy, impact_map):
    result = _resolve(repo, policy, impact_map, [])
    assert result["mode"] == "full"
    assert result["reason"] == "no_changed_file_scope"


def test_global_path_forces_full(repo, policy, impact_map):
    result = _resolve(repo, policy, impact_map, ["src/mac/foo.py", "tests/conftest.py"])
    assert result["mode"] == "full"
    assert result["reason"] == "global_infrastructure_changed"
    assert result["global_files"] == ["tests/conftest.py"]


def test_opaque_infra_file_forces_full(repo, policy, impact_map):
    for opaque in ("scripts/deploy.sh", "Makefile", "ci.yaml", "src/mac/data/x.json"):
        result = _resolve(repo, policy, impact_map, [opaque])
        assert result["mode"] == "full", opaque
        assert result["reason"] == "unmappable_non_code_change"


def test_reviewed_opaque_path_selects_owning_contract_and_guards(
    repo, policy, impact_map
):
    result = _resolve(
        repo,
        policy,
        impact_map,
        ["src/mac/data/env_config_registry.json"],
    )
    assert result["mode"] == "focused"
    assert result["reason"] == "impact_hybrid_scope"
    assert set(result["tests"]) == {
        "tests/test_env_config.py",
        "tests/test_always.py",
        "tests/test_canary.py",
    }


def test_reviewed_documentation_path_runs_its_contract(repo, policy, impact_map):
    result = _resolve(repo, policy, impact_map, ["docs/env-config-reference.md"])
    assert result["mode"] == "focused"
    assert "tests/test_env_config.py" in result["tests"]


def test_missing_reviewed_contract_test_fails_closed(repo, policy, impact_map):
    (repo / "tests/test_env_config.py").unlink()
    result = _resolve(
        repo,
        policy,
        impact_map,
        ["src/mac/data/env_config_registry.json"],
    )
    assert result["mode"] == "full"
    assert result["reason"] == "path_test_contract_missing"
    assert result["missing_contract_tests"] == {
        "src/mac/data/env_config_registry.json": ["tests/test_env_config.py"]
    }


def test_selector_has_a_reviewed_self_contract(repo, policy, impact_map):
    result = _resolve(repo, policy, impact_map, ["scripts/resolve-impacted-tests.py"])
    assert result["mode"] == "focused"
    assert "tests/test_resolve_impacted_tests.py" in result["tests"]


def test_unmappable_source_entry_point_resolves_by_contract_without_codegraph(
    repo, policy, impact_map
):
    """A source entry point runs only out-of-process, so the coverage map never
    attributes it. Its reviewed path contract must resolve it to its dedicated
    test even when CodeGraph is unavailable — never a full-suite escalation. The
    fixture map does not map ``git_askpass.py``, so without the contract this
    would fail closed to full at the unresolved-source guard."""
    (repo / "tests/test_git_askpass.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    result = _resolve(
        repo,
        policy,
        impact_map,
        ["src/mac/git_askpass.py"],
        cg=(),
        cg_problem="codegraph_unavailable",
    )
    assert result["mode"] == "focused"
    assert result["reason"] == "impact_hybrid_scope"
    assert "tests/test_git_askpass.py" in result["tests"]
    # Cross-cutting guards still ride along with the real source change.
    assert "tests/test_always.py" in result["tests"]
    assert "tests/test_canary.py" in result["tests"]


def test_missing_source_entry_point_contract_test_fails_closed(repo, policy, impact_map):
    """The existence guard applies to source contracts too: if the reviewed test
    is gone, the resolver must fail closed rather than silently drop coverage."""
    # tests/test_git_askpass.py is intentionally absent from this tmp repo.
    result = _resolve(repo, policy, impact_map, ["src/mac/git_askpass.py"])
    assert result["mode"] == "full"
    assert result["reason"] == "path_test_contract_missing"
    assert result["missing_contract_tests"] == {
        "src/mac/git_askpass.py": ["tests/test_git_askpass.py"]
    }


def test_documentation_only_selects_no_tests(repo, policy, impact_map):
    result = _resolve(repo, policy, impact_map, ["docs/guide.md", "README rename.md"])
    assert result["mode"] == "focused"
    assert result["reason"] == "non_code_change"
    assert result["tests"] == []


def test_line_level_hit_selects_only_intersecting_tests(repo, policy, impact_map):
    # Change line 10 of foo.py -> only test_a executed line 10.
    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {10}}
    )
    assert result["mode"] == "focused"
    assert "tests/test_foo.py::test_a" in result["tests"]
    assert "tests/test_foo.py::test_b" not in result["tests"]
    # Cross-cutting guards ride along.
    assert "tests/test_always.py" in result["tests"]
    assert "tests/test_canary.py" in result["tests"]


def test_shared_line_selects_both_tests(repo, policy, impact_map):
    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {12}}
    )
    assert {"tests/test_foo.py::test_a", "tests/test_foo.py::test_b"} <= set(result["tests"])


def test_additions_only_falls_back_to_file_level(repo, policy, impact_map):
    # No base lines for the changed file (pure additions) -> every test that
    # touched the file at base is selected.
    result = _resolve(repo, policy, impact_map, ["src/mac/foo.py"], {})
    assert {"tests/test_foo.py::test_a", "tests/test_foo.py::test_b"} <= set(result["tests"])


def test_stale_map_uses_codegraph(repo, policy, impact_map):
    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {10}},
        fresh=False, cg=["tests/test_bar.py::test_c"],
    )
    assert result["mode"] == "focused"
    assert result["map_fresh"] is False
    assert "tests/test_bar.py::test_c" in result["tests"]


def test_stale_map_without_codegraph_fails_closed(repo, policy, impact_map):
    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {10}},
        fresh=False, cg=(), cg_problem="codegraph_unavailable",
    )
    assert result["mode"] == "full"
    assert result["reason"] == "codegraph_unavailable"


def test_stale_map_with_empty_codegraph_fails_closed(repo, policy, impact_map):
    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {10}},
        fresh=False, cg=(), cg_problem=None,
    )
    assert result["mode"] == "full"
    assert result["reason"] == "unresolved_source_without_reliable_affected_tests"


def test_new_source_file_not_in_map_fails_closed_without_codegraph(repo, policy, impact_map):
    result = _resolve(
        repo, policy, impact_map, ["src/mac/brand_new.py"], {"src/mac/brand_new.py": {1}},
        fresh=True, cg=(), cg_problem=None,
    )
    assert result["mode"] == "full"


def test_changed_test_file_is_selected_directly(repo, policy, impact_map):
    result = _resolve(repo, policy, impact_map, ["tests/test_changed.py"])
    assert result["mode"] == "focused"
    assert "tests/test_changed.py" in result["tests"]


def test_nonexistent_always_run_is_filtered(repo, policy, impact_map):
    impact_map["always_run"] = ["tests/test_does_not_exist.py"]
    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {10}}
    )
    assert "tests/test_does_not_exist.py" not in result["tests"]


def test_missing_map_treats_all_source_as_unresolved(repo, policy):
    # No map at all: a source change relies entirely on codegraph; none -> full.
    result = _resolve(
        repo, policy, None, ["src/mac/foo.py"], {"src/mac/foo.py": {10}},
        fresh=False, cg=(), cg_problem=None,
    )
    assert result["mode"] == "full"


# --------------------------------------------------------------------------
# Ancestor-tolerant, per-file freshness (_fresh_map_files, git-backed)
# --------------------------------------------------------------------------


def _git_repo(root: Path):
    def g(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=True
        ).stdout.strip()

    g("init", "-q")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "Tester")
    g("config", "commit.gpgsign", "false")
    return g


def _commit_file(g, root: Path, rel: str, content: str) -> str:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    g("add", rel)
    g("commit", "-qm", f"touch {rel}")
    return g("rev-parse", "HEAD")


@pytest.fixture()
def mapped_repo(tmp_path: Path):
    """A real git repo with two mapped source files committed at a base SHA."""
    g = _git_repo(tmp_path)
    _commit_file(g, tmp_path, "src/mac/foo.py", "x = 1\n")
    base = _commit_file(g, tmp_path, "src/mac/bar.py", "y = 2\n")
    impact_map = {
        "base_sha": base,
        "file_tests": {"src/mac/foo.py": [0], "src/mac/bar.py": [1]},
    }
    return tmp_path, g, base, impact_map


def test_fresh_files_exact_base_returns_all_mapped(mapped_repo):
    root, _g, base, impact_map = mapped_repo
    fresh = R._fresh_map_files(impact_map, base, root)
    assert fresh == frozenset({"src/mac/foo.py", "src/mac/bar.py"})


def test_fresh_files_ancestor_drops_only_the_changed_file(mapped_repo):
    root, g, _base, impact_map = mapped_repo
    # Advance main: change ONLY bar.py. foo.py is byte-identical to base.
    newer = _commit_file(g, root, "src/mac/bar.py", "y = 3\n")
    fresh = R._fresh_map_files(impact_map, newer, root)
    # foo.py unchanged since base_sha -> still trustworthy; bar.py changed -> dropped.
    assert fresh == frozenset({"src/mac/foo.py"})


def test_fresh_files_non_ancestor_base_is_empty(mapped_repo):
    root, g, base, impact_map = mapped_repo
    # A map built at a DESCENDANT of the selection base is not an ancestor of it.
    newer = _commit_file(g, root, "src/mac/foo.py", "x = 9\n")
    stale_map = {**impact_map, "base_sha": newer}
    assert R._fresh_map_files(stale_map, base, root) == frozenset()


def test_fresh_files_handles_missing_inputs(mapped_repo):
    root, _g, base, impact_map = mapped_repo
    assert R._fresh_map_files(None, base, root) == frozenset()
    assert R._fresh_map_files(impact_map, None, root) == frozenset()
    assert R._fresh_map_files({"file_tests": {"src/x.py": [0]}}, base, root) == frozenset()


def test_ancestor_freshness_end_to_end_line_selection(mapped_repo):
    """Integration: on an ancestor base where only bar.py changed, a foo.py change
    still resolves at line level from the map (map stays useful as main moves)."""
    root, g, _base, impact_map = mapped_repo
    impact_map["nodeids"] = ["tests/test_foo.py::test_a"]
    impact_map["file_line_tests"] = {"src/mac/foo.py": {"1": [0]}}
    newer = _commit_file(g, root, "src/mac/bar.py", "y = 3\n")

    fresh = R._fresh_map_files(impact_map, newer, root)
    result = R.resolve(
        ["src/mac/foo.py"],
        {"src/mac/foo.py": {1}},
        fresh_map_files=fresh,
        policy=R.SelectionPolicy(),
        impact_map=impact_map,
        codegraph_tests=[],
        codegraph_problem="codegraph_unavailable",
        repo_root=root,
    )
    assert result["mode"] == "focused"
    assert result["map_fresh"] is True
    assert result["tests"] == ["tests/test_foo.py::test_a"]
