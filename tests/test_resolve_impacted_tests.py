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
    # Each file defines the tests the impact map below actually names. The
    # resolver drops node ids it cannot collect, so a fixture whose map points
    # at functions the files do not define would exercise the stale-entry path
    # instead of the selection logic these tests are about.
    bodies = {
        "tests/test_foo.py": ("test_a", "test_b"),
        "tests/test_bar.py": ("test_c",),
    }
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
        names = bodies.get(rel, ("test_x",))
        target.write_text(
            "".join("def %s():\n    assert True\n\n\n" % name for name in names),
            encoding="utf-8",
        )
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


def test_fleet_installer_contract_includes_every_direct_test_owner():
    direct_owners = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("test_*.py")
        if "fleet-node-install.sh" in path.read_text(encoding="utf-8")
    }
    direct_owners.discard("tests/test_resolve_impacted_tests.py")

    assert direct_owners <= set(
        R.PATH_TEST_CONTRACTS["deploy/fleet-node-install.sh"]
    )


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


def test_stale_map_without_codegraph_fails_closed_on_unresolved_source(repo, policy, impact_map):
    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {10}},
        fresh=False, cg=(), cg_problem="codegraph_unavailable",
    )
    assert result["mode"] == "full"
    assert result["reason"] == "unresolved_source_without_reliable_affected_tests"


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
    # The map's node ids must be collectable, or the resolver drops them as
    # stale and the selection under test never happens.
    _commit_file(
        g, tmp_path, "tests/test_foo.py",
        "def test_a():\n    assert True\n\n\ndef test_b():\n    assert True\n",
    )
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
    assert "tests/test_foo.py::test_a" in result["tests"]
    assert "tests/test_foo.py::test_b" not in result["tests"]


def test_resolver_drops_node_ids_that_pytest_can_no_longer_collect(tmp_path):
    """A stale impact map must cost precision, never break the run.

    The map is a committed artifact, so a renamed or deleted test stays in it
    until it is rebuilt. Handing pytest a node id that no longer resolves is a
    USAGE error (exit 4), not a test failure, so one stale entry took down the
    sanity job of whoever next touched that file -- reporting the missing test
    rather than the stale map. Observed live after a rename in #256.
    """

    suite = tmp_path / "tests"
    suite.mkdir()
    (suite / "test_sample.py").write_text(
        "def test_present():\n    pass\n\n\nclass TestGroup:\n"
        "    def test_in_class(self):\n        pass\n"
    )

    kept, dropped = R._resolvable(
        [
            "tests/test_sample.py::test_present",
            "tests/test_sample.py::test_present[param]",
            "tests/test_sample.py::TestGroup::test_in_class",
            "tests/test_sample.py::test_renamed_away",
            "tests/test_deleted_file.py::test_anything",
            "tests/test_sample.py",
        ],
        tmp_path,
    )
    assert kept == [
        "tests/test_sample.py::test_present",
        "tests/test_sample.py::test_present[param]",
        "tests/test_sample.py::TestGroup::test_in_class",
        "tests/test_sample.py",
    ]
    assert dropped == [
        "tests/test_sample.py::test_renamed_away",
        "tests/test_deleted_file.py::test_anything",
    ]


# ---------------------------------------------------------------------------
# Scope-level resolution.
#
# Two independent reasons a one-line change used to select all 11,020 tests --
# an hour of CI each, sixteen times in the last sixty commits on main, every one
# of them src/mac/cli.py:
#
#   drift    the line index is usable only for files byte-identical to the map's
#            revision. A file that changes most weeks is almost never identical,
#            and one unresolvable file takes the whole suite with it.
#   pruning  lines executed by more than the fanout cap are dropped from the
#            line index -- exactly the widely-executed ones.
#
# The scope index is keyed by qualified name (so drift does not invalidate it)
# and aggregated before the prune (so the hot lines still have an answer).
# ---------------------------------------------------------------------------


@pytest.fixture()
def scoped_map(impact_map) -> dict:
    scoped = dict(impact_map)
    scoped["file_scope_tests"] = {
        "src/mac/foo.py": {"build_parser": [0], "OtherClass.method": [1]},
    }
    return scoped


def _write_source(repo, rel, body):
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")


def test_a_drifted_file_resolves_by_scope_instead_of_going_full(
    repo, policy, scoped_map, monkeypatch
):
    """The case that cost the most: a mapped file whose line numbers moved."""
    monkeypatch.setattr(
        R, "touched_scope_names", lambda *a, **k: {"build_parser"}
    )

    result = _resolve(repo, policy, scoped_map, ["src/mac/foo.py"], fresh=False)

    assert result["mode"] == "focused", result.get("reason")
    assert "tests/test_foo.py::test_a" in result["tests"]


def test_only_the_touched_scope_is_charged(repo, policy, scoped_map, monkeypatch):
    """Otherwise scope resolution is just the file answer with extra steps."""
    monkeypatch.setattr(
        R, "touched_scope_names", lambda *a, **k: {"OtherClass.method"}
    )

    result = _resolve(repo, policy, scoped_map, ["src/mac/foo.py"], fresh=False)

    # always-run entries are unioned in regardless; what matters is that the
    # OTHER scope's test is absent.
    assert "tests/test_foo.py::test_b" in result["tests"]
    assert "tests/test_foo.py::test_a" not in result["tests"]


def test_a_module_level_change_still_goes_full(repo, policy, scoped_map, monkeypatch):
    """Module-level code runs at import for every importer, so nothing narrower
    than the file is honest -- and a drifted file has no file answer either."""
    monkeypatch.setattr(R, "touched_scope_names", lambda *a, **k: None)

    result = _resolve(repo, policy, scoped_map, ["src/mac/foo.py"], fresh=False)

    assert result["mode"] == "full"


def test_a_scope_the_map_never_saw_charges_nothing(repo, policy, scoped_map, monkeypatch):
    """New code cannot have tests attributed to it. Whatever calls it is part
    of the same diff and charged wherever it landed."""
    monkeypatch.setattr(R, "touched_scope_names", lambda *a, **k: {"brand_new"})

    result = _resolve(repo, policy, scoped_map, ["src/mac/foo.py"], fresh=False)

    assert result["mode"] == "focused"
    assert "tests/test_foo.py::test_a" not in result["tests"]


def test_a_pruned_line_falls_back_to_the_file_rather_than_selecting_nothing(
    repo, policy, impact_map, monkeypatch
):
    """The builder documents this fallback and the resolver did not implement
    it: a line missing from the index contributed NOTHING, so a change to the
    most widely-executed code selected the fewest tests. Line 99 is not in the
    index, standing in for a line pruned for high fanout."""
    monkeypatch.setattr(R, "touched_scope_names", lambda *a, **k: None)

    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {99}}
    )

    assert result["mode"] == "focused"
    assert {"tests/test_foo.py::test_a", "tests/test_foo.py::test_b"} <= set(
        result["tests"]
    )


def test_a_known_line_is_still_answered_by_line(repo, policy, impact_map, monkeypatch):
    """The narrow answer must not be lost to the new fallback."""
    monkeypatch.setattr(R, "touched_scope_names", lambda *a, **k: None)

    result = _resolve(
        repo, policy, impact_map, ["src/mac/foo.py"], {"src/mac/foo.py": {10}}
    )

    assert "tests/test_foo.py::test_a" in result["tests"]
    assert "tests/test_foo.py::test_b" not in result["tests"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _sandbox_shaped_repo(tmp_path: Path) -> tuple[Path, str]:
    """A worktree shaped the way a task sandbox shapes one.

    `git init`, one baseline commit, and the agent's work left UNCOMMITTED --
    which is the real arrangement: the host finalizer is what commits, so
    inside the sandbox HEAD is the baseline itself.
    """
    repo = tmp_path / "sandbox"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "mac-sandbox@invalid")
    _git(repo, "config", "user.name", "MAC OpenShell sandbox")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src" / "thing.py").write_text("value = 1\n", encoding="utf-8")
    (repo / "tests" / "test_thing.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "MAC OpenShell sandbox baseline")
    baseline = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return repo, baseline


def test_uncommitted_work_is_still_this_task_s_changes(tmp_path: Path):
    """The live failure: every task selected `full` because its diff looked
    empty.

    `base...HEAD` compares COMMITS. In a sandbox the agent's work is in the
    working tree and HEAD is the base, so the range is empty, the selector
    reports no changed files, and the gate escalates to the whole repository --
    which then dies in its parallel phase. The task's work is real; only the
    range was wrong.
    """
    repo, baseline = _sandbox_shaped_repo(tmp_path)
    (repo / "tests" / "test_thing.py").write_text(
        "def test_a():\n    pass\n\n\ndef test_b():\n    pass\n", encoding="utf-8"
    )

    assert R.git_changed_files(baseline, repo) == ["tests/test_thing.py"]


def test_an_added_file_counts_even_though_it_is_untracked(tmp_path: Path):
    """A task told to ADD a test leaves it untracked, and `git diff` alone
    never mentions it -- so the selection would silently omit the very file
    the task exists to create."""
    repo, baseline = _sandbox_shaped_repo(tmp_path)
    (repo / "tests" / "test_new.py").write_text("def test_c():\n    pass\n", encoding="utf-8")

    assert R.git_changed_files(baseline, repo) == ["tests/test_new.py"]


def test_committed_work_still_wins(tmp_path: Path):
    """CI commits before it selects. The working-tree fallback must not
    displace the commit range there, or an unrelated dirty file on a developer's
    machine would widen the selection."""
    repo, baseline = _sandbox_shaped_repo(tmp_path)
    (repo / "src" / "thing.py").write_text("value = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "the agent's work")
    (repo / "tests" / "test_thing.py").write_text("def test_z():\n    pass\n", encoding="utf-8")

    assert R.git_changed_files(baseline, repo) == ["src/thing.py"]


def test_changed_lines_are_charged_for_uncommitted_work(tmp_path: Path):
    """Without the same range fix, every changed file resolves with no line
    information and is charged whole-file -- which selects far more than the
    diff touched, for exactly the tasks the impact path exists to keep small."""
    repo, baseline = _sandbox_shaped_repo(tmp_path)
    (repo / "src" / "thing.py").write_text("value = 1\nextra = 2\n", encoding="utf-8")

    lines, additions = R.changed_base_lines(baseline, repo)

    assert lines or additions
