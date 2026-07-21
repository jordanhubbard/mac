"""Safety-matrix unit tests for scripts/resolve-impacted-tests.py.

Every path is driven through the pure ``resolve()`` function so no git or real
coverage run is required. The bar is fail-closed: whenever a changed file cannot
be safely mapped, the resolver must escalate to a full run.
"""

from __future__ import annotations

import importlib.util
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
    return R.resolve(
        changed,
        base_lines or {},
        resolved_base_sha=BASE_SHA if fresh else "b" * 40,
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
