"""Contract tests for test-gate checkpointing (src/mac/test_checkpoint.py).

The property under test is not "resuming is fast". It is "resuming never skips a
test that would now fail". Every case below is written from that direction: a
checkpoint must be invalidated when it must be, a previously-failing test must
still be run and still fail, and anything unreadable, stale, or ambiguous must
produce a full run rather than a narrower one.
"""

from __future__ import annotations

import json
import sys
import subprocess
from pathlib import Path

import pytest

from mac import test_checkpoint as tc


# --------------------------------------------------------------------------
# A miniature repository, so the git-facing parts are exercised for real.
# --------------------------------------------------------------------------


def _git(repo: Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *argv], cwd=str(repo), capture_output=True, text=True, check=True
    )


@pytest.fixture()
def repo(tmp_path: Path, request) -> Path:
    """A tiny git repo whose layout mirrors the real one closely enough.

    It carries a real copy of scripts/resolve-impacted-tests.py and a synthetic
    impact map, so the charging rules under test are the production ones.
    """
    root = tmp_path / "repo"
    (root / "src" / "mac" / "data").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "tests").mkdir()
    (root / "docs").mkdir()

    real_root = Path(__file__).resolve().parents[1]
    (root / "scripts" / "resolve-impacted-tests.py").write_text(
        (real_root / "scripts" / "resolve-impacted-tests.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "scripts" / "run-contract-tests.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (root / "conftest.py").write_text("", encoding="utf-8")
    (root / "tests" / "conftest.py").write_text("", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (root / "uv.lock").write_text("", encoding="utf-8")
    (root / "src" / "mac" / "test_checkpoint.py").write_text("", encoding="utf-8")
    (root / "test-policy.toml").write_text(
        "[selection]\n"
        'global_full_paths = ["conftest.py", "tests/conftest.py", "test-policy.toml", "Makefile"]\n'
        'always_run = ["tests/test_guard.py"]\n'
        'impact_map = "src/mac/data/test_impact_map.json"\n',
        encoding="utf-8",
    )

    (root / "src" / "mac" / "alpha.py").write_text("A = 1\n", encoding="utf-8")
    (root / "src" / "mac" / "beta.py").write_text("B = 1\n", encoding="utf-8")
    (root / "tests" / "test_alpha.py").write_text("def test_a():\n    pass\n", encoding="utf-8")
    (root / "tests" / "test_beta.py").write_text("def test_b():\n    pass\n", encoding="utf-8")
    (root / "tests" / "test_guard.py").write_text("def test_g():\n    pass\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("# guide\n", encoding="utf-8")

    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "initial")
    _write_map(root)
    return root


def _write_map(root: Path) -> dict:
    """An impact map built from the tree exactly as it stands right now."""
    nodeids = [
        "tests/test_alpha.py::test_a",
        "tests/test_beta.py::test_b",
    ]
    document = {
        "schema": "mac.test_impact_map.v1",
        "base_sha": _git(root, "rev-parse", "HEAD").stdout.strip(),
        "source_prefix": "src/",
        "nodeids": nodeids,
        "file_tests": {"src/mac/alpha.py": [0], "src/mac/beta.py": [1]},
        "file_line_tests": {},
        "file_scope_tests": {},
        "file_hashes": {
            name: tc._sha256_file(root / name)
            for name in ("src/mac/alpha.py", "src/mac/beta.py")
        },
        "always_run": [],
        "stats": {},
    }
    path = root / "src" / "mac" / "data" / "test_impact_map.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return document


def _resolver(repo: Path):
    # Force a fresh import bound to THIS repo, not the process-wide cache.
    import sys

    sys.modules.pop(tc._RESOLVER_NAME, None)
    module = tc.load_resolver(repo)
    assert module is not None
    return module


def _record(repo: Path, directory: Path, outcomes: dict[str, str]) -> None:
    resolver = _resolver(repo)
    impact_map = resolver.load_map(repo / "src" / "mac" / "data" / "test_impact_map.json")
    document = tc.build_checkpoint(
        repo_root=repo, outcomes=outcomes, gate="test", impact_map=impact_map, env={}
    )
    assert document is not None
    tc.write_checkpoint(directory, document)


def _plan(repo: Path, directory: Path, **kwargs) -> tc.Plan:
    resolver = _resolver(repo)
    policy = resolver.load_policy(repo / "test-policy.toml")
    policy = resolver.SelectionPolicy(
        global_full_paths=policy.global_full_paths,
        always_run=policy.always_run,
        map_path=repo / "src" / "mac" / "data" / "test_impact_map.json",
    )
    impact_map = resolver.load_map(policy.map_path)
    kwargs.setdefault("require_whole_coverage", False)
    return tc.plan(
        repo_root=repo,
        directory=directory,
        resolver=resolver,
        policy=policy,
        impact_map=impact_map,
        env={},
        **kwargs,
    )


GREEN = {
    "tests/test_alpha.py::test_a": "passed",
    "tests/test_beta.py::test_b": "passed",
    "tests/test_guard.py::test_g": "passed",
}


# --------------------------------------------------------------------------
# Fail open: no checkpoint, a corrupt one, or a truncated one runs everything.
# --------------------------------------------------------------------------


def test_absent_checkpoint_runs_the_full_suite(repo, tmp_path):
    plan = _plan(repo, tmp_path / "ckpt")
    assert plan.mode == "full"
    assert plan.reason == "no_usable_checkpoint"
    assert plan.skip_files == ()


@pytest.mark.parametrize(
    "payload",
    [
        "{ this is not json",
        "[]",
        json.dumps({"schema": "something.else.v1"}),
        json.dumps({"schema": tc.SCHEMA, "files": {}}),  # no tree
        json.dumps({"schema": tc.SCHEMA, "tree": {}}),  # no files
    ],
)
def test_corrupt_checkpoint_runs_the_full_suite(repo, tmp_path, payload):
    directory = tmp_path / "ckpt"
    directory.mkdir()
    tc.checkpoint_path(directory).write_text(payload, encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "full", "a checkpoint we cannot read must never narrow the run"
    assert plan.skip_files == ()


def test_a_checkpoint_that_recorded_nothing_runs_the_full_suite(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, {})
    # build_checkpoint with no outcomes still yields a document; it must not
    # be mistaken for "everything passed".
    plan = _plan(repo, directory)
    assert plan.mode == "full"
    assert plan.reason == "checkpoint_recorded_no_tests"


# --------------------------------------------------------------------------
# Invalidation.
# --------------------------------------------------------------------------


def test_identical_tree_carries_every_passing_file_forward(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    plan = _plan(repo, directory)
    assert plan.mode == "resume"
    # test_guard.py is always_run and is never carried forward, however green.
    assert set(plan.skip_files) == {"tests/test_alpha.py", "tests/test_beta.py"}
    assert plan.skip_tests == 2


def test_always_run_files_are_never_carried_forward(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    plan = _plan(repo, directory)
    assert "tests/test_guard.py" not in plan.skip_files


def test_changing_a_source_file_re_runs_the_tests_the_map_charges_to_it(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "src" / "mac" / "alpha.py").write_text("A = 2\n", encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "resume"
    assert "tests/test_alpha.py" not in plan.skip_files, (
        "the impact map charges test_alpha to alpha.py, so it must re-run"
    )
    assert plan.skip_files == ("tests/test_beta.py",)
    assert "src/mac/alpha.py" in plan.delta


def test_changing_a_test_file_re_runs_that_test_file(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "tests" / "test_beta.py").write_text(
        "def test_b():\n    assert False\n", encoding="utf-8"
    )
    plan = _plan(repo, directory)
    assert plan.mode == "resume"
    assert "tests/test_beta.py" not in plan.skip_files
    assert plan.skip_files == ("tests/test_alpha.py",)


def test_a_globally_invalidating_file_forces_the_full_suite(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    # `Makefile` is in this policy's global_full_paths and is deliberately NOT
    # in the runner fingerprint, so the global-path rule is what is under test
    # here rather than the coarser fingerprint check.
    (repo / "Makefile").write_text("all:\n", encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "full"
    assert plan.reason == "global_infrastructure_changed"


def test_a_fingerprinted_config_file_also_forces_the_full_suite(repo, tmp_path):
    """conftest.py is both fingerprinted and globally invalidating: either rule
    alone must produce a full run, so the outcome is the same whichever fires."""
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "tests" / "conftest.py").write_text("# changed\n", encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "full"
    assert plan.skip_files == ()


def test_an_opaque_non_code_file_forces_the_full_suite(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "deploy.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "full"
    assert plan.reason == "unmappable_non_code_change"


def test_a_new_source_file_the_map_never_saw_forces_the_full_suite(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "src" / "mac" / "gamma.py").write_text("G = 1\n", encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "full"
    assert plan.reason == "source_file_absent_from_impact_map"


def test_a_map_built_from_other_bytes_forces_the_full_suite(repo, tmp_path):
    """The map's knowledge of a changed file must date from the checkpoint tree.

    If the map was built from different bytes of that file, the tests it
    attributes are not the tests that ran, and nothing can be carried forward.
    """
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    document = json.loads(tc.checkpoint_path(directory).read_text(encoding="utf-8"))
    document["map_source_hashes"]["src/mac/alpha.py"] = "sha256:" + "0" * 64
    tc.checkpoint_path(directory).write_text(json.dumps(document), encoding="utf-8")
    (repo / "src" / "mac" / "alpha.py").write_text("A = 3\n", encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "full"
    assert plan.reason == "impact_map_stale_for_changed_file"


def test_documentation_alone_carries_everything_forward(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "docs" / "guide.md").write_text("# guide, revised\n", encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "resume"
    assert set(plan.skip_files) == {"tests/test_alpha.py", "tests/test_beta.py"}


def test_runner_fingerprint_change_discards_the_whole_checkpoint(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    # A change to the runner itself: what the gate DOES may now differ, so no
    # recorded outcome can be trusted, regardless of the tree delta.
    (repo / "scripts" / "run-contract-tests.sh").write_text("#!/bin/sh\n# v2\n", encoding="utf-8")
    plan = _plan(repo, directory)
    assert plan.mode == "full"
    assert plan.reason == "runner_fingerprint_changed"


def test_gate_shaping_environment_is_part_of_the_key(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    resolver = _resolver(repo)
    impact_map = resolver.load_map(repo / "src" / "mac" / "data" / "test_impact_map.json")
    policy = resolver.SelectionPolicy(
        global_full_paths=frozenset(), always_run=(), map_path=Path("/nonexistent")
    )
    plan = tc.plan(
        repo_root=repo,
        directory=directory,
        require_whole_coverage=False,
        resolver=resolver,
        policy=policy,
        impact_map=impact_map,
        env={"MAC_TEST_DISABLE_GROUPS": "fleet"},
    )
    assert plan.mode == "full"
    assert plan.reason == "runner_fingerprint_changed"


# --------------------------------------------------------------------------
# A previously-failing test is always re-run.
# --------------------------------------------------------------------------


def test_a_previously_failing_file_is_always_re_run(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(
        repo,
        directory,
        {
            "tests/test_alpha.py::test_a": "passed",
            "tests/test_beta.py::test_b": "failed",
            "tests/test_guard.py::test_g": "passed",
        },
    )
    plan = _plan(repo, directory)
    assert plan.mode == "resume"
    assert "tests/test_beta.py" not in plan.skip_files
    assert plan.previously_failed == ("tests/test_beta.py::test_b",)


def test_one_failure_re_runs_the_whole_file(repo, tmp_path):
    """Half a module never runs alone: module-scoped fixture state differs."""
    directory = tmp_path / "ckpt"
    _record(
        repo,
        directory,
        {
            "tests/test_beta.py::test_b": "failed",
            "tests/test_beta.py::test_b2": "passed",
            "tests/test_alpha.py::test_a": "passed",
        },
    )
    plan = _plan(repo, directory)
    assert plan.skip_files == ("tests/test_alpha.py",)


def test_a_skipped_outcome_is_not_carried_forward(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(
        repo,
        directory,
        {
            "tests/test_alpha.py::test_a": "passed",
            "tests/test_beta.py::test_b": "skipped",
        },
    )
    plan = _plan(repo, directory)
    assert plan.skip_files == ("tests/test_alpha.py",)


def test_a_deleted_test_file_is_not_carried_forward(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "tests" / "test_beta.py").unlink()
    plan = _plan(repo, directory)
    assert "tests/test_beta.py" not in plan.skip_files


# --------------------------------------------------------------------------
# Coverage: a resumed run may never satisfy the whole-repo floors.
# --------------------------------------------------------------------------


def test_whole_repo_coverage_downgrades_a_resume_to_a_triage_pass(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    plan = _plan(repo, directory, require_whole_coverage=True)
    assert plan.mode == "resume"
    assert plan.reason == "triage_pass_only"
    assert plan.coverage_authoritative is False
    assert any("whole-repo coverage floors" in note for note in plan.notes)


def test_a_non_coverage_gate_resume_is_authoritative(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    plan = _plan(repo, directory, require_whole_coverage=False)
    assert plan.mode == "resume"
    assert plan.coverage_authoritative is True


def test_a_full_plan_is_never_marked_non_authoritative(repo, tmp_path):
    plan = _plan(repo, tmp_path / "ckpt", require_whole_coverage=True)
    assert plan.mode == "full"
    assert plan.coverage_authoritative is True


# --------------------------------------------------------------------------
# Observability.
# --------------------------------------------------------------------------


def test_the_plan_explains_itself(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "src" / "mac" / "alpha.py").write_text("A = 9\n", encoding="utf-8")
    rendered = tc.render_plan(_plan(repo, directory))
    assert "test checkpoint: resume" in rendered
    assert "changed since checkpoint: src/mac/alpha.py" in rendered
    assert "carrying forward 1 test files (1 tests)" in rendered
    assert "re-running" in rendered


def test_a_full_plan_says_so(repo, tmp_path):
    rendered = tc.render_plan(_plan(repo, tmp_path / "ckpt"))
    assert "running the complete suite (checkpointing declined)" in rendered


# --------------------------------------------------------------------------
# Result ingestion.
# --------------------------------------------------------------------------


def test_results_merge_across_workers_with_failure_winning(tmp_path):
    directory = tmp_path / "results"
    directory.mkdir()
    (directory / "gw0.jsonl").write_text(
        json.dumps({"nodeid": "t.py::a", "outcome": "passed"}) + "\n"
        + json.dumps({"nodeid": "t.py::b", "outcome": "passed"}) + "\n",
        encoding="utf-8",
    )
    (directory / "gw1.jsonl").write_text(
        json.dumps({"nodeid": "t.py::b", "outcome": "failed"}) + "\n"
        + "not json at all\n"
        + json.dumps({"nodeid": "t.py::c", "outcome": "bogus"}) + "\n",
        encoding="utf-8",
    )
    assert tc.ingest_results(directory) == {"t.py::a": "passed", "t.py::b": "failed"}


def test_missing_results_directory_yields_no_outcomes(tmp_path):
    assert tc.ingest_results(tmp_path / "nope") == {}


# --------------------------------------------------------------------------
# Carrying the chain across successive resumes.
# --------------------------------------------------------------------------


def test_a_resumed_run_keeps_the_files_it_carried_forward(repo, tmp_path):
    """Otherwise every resume is a one-shot: the second run would have nothing.

    File-level charging is content-based and therefore composes, so a file that
    survived A->B and then B->C has survived A->C.
    """
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    previous = tc.load_checkpoint(directory)

    resolver = _resolver(repo)
    impact_map = resolver.load_map(repo / "src" / "mac" / "data" / "test_impact_map.json")
    fresh = tc.build_checkpoint(
        repo_root=repo,
        outcomes={"tests/test_guard.py::test_g": "passed"},
        gate="test",
        impact_map=impact_map,
        env={},
    )
    merged = tc.merge_carried_forward(
        fresh, previous, {"tests/test_alpha.py", "tests/test_beta.py"}, repo_root=repo
    )
    assert merged is not None
    assert set(merged["files"]) == {
        "tests/test_alpha.py",
        "tests/test_beta.py",
        "tests/test_guard.py",
    }
    assert merged["carried_forward_files"] == 2


def test_carrying_forward_refuses_a_checkpoint_from_a_different_runner(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    previous = tc.load_checkpoint(directory)
    assert previous is not None
    previous["runner_fingerprint"] = "sha256:" + "1" * 64
    fresh = tc.build_checkpoint(
        repo_root=repo, outcomes=GREEN, gate="test", impact_map=None, env={}
    )
    assert (
        tc.merge_carried_forward(fresh, previous, {"tests/test_alpha.py"}, repo_root=repo) is None
    )


def test_carrying_forward_never_promotes_a_failure(repo, tmp_path):
    directory = tmp_path / "ckpt"
    _record(repo, directory, {"tests/test_beta.py::test_b": "failed"})
    previous = tc.load_checkpoint(directory)
    fresh = tc.build_checkpoint(
        repo_root=repo,
        outcomes={"tests/test_alpha.py::test_a": "passed"},
        gate="test",
        impact_map=None,
        env={},
    )
    merged = tc.merge_carried_forward(fresh, previous, {"tests/test_beta.py"}, repo_root=repo)
    assert merged is not None
    assert "tests/test_beta.py" not in merged["files"]


# --------------------------------------------------------------------------
# Tree manifest.
# --------------------------------------------------------------------------


def test_the_manifest_tracks_unstaged_edits_and_untracked_files(repo):
    before = tc.tree_manifest(repo)
    assert before is not None
    assert before["src/mac/alpha.py"]
    (repo / "src" / "mac" / "alpha.py").write_text("A = 42\n", encoding="utf-8")
    (repo / "new_file.txt").write_text("hello\n", encoding="utf-8")
    after = tc.tree_manifest(repo)
    assert after is not None
    assert after["src/mac/alpha.py"] != before["src/mac/alpha.py"]
    assert "new_file.txt" in after and "new_file.txt" not in before


def test_the_manifest_drops_files_deleted_from_the_worktree(repo):
    before = tc.tree_manifest(repo)
    (repo / "src" / "mac" / "beta.py").unlink()
    after = tc.tree_manifest(repo)
    assert "src/mac/beta.py" in (before or {})
    assert "src/mac/beta.py" not in (after or {})


def test_no_manifest_outside_a_git_repo_means_no_checkpoint(tmp_path):
    assert tc.tree_manifest(tmp_path) is None
    assert (
        tc.build_checkpoint(
            repo_root=tmp_path, outcomes={"a::b": "passed"}, gate="x", impact_map=None, env={}
        )
        is None
    )


# --------------------------------------------------------------------------
# The reviewed path contract that motivated this work.
# --------------------------------------------------------------------------


def test_mkdocs_yml_has_an_owning_test_instead_of_forcing_the_full_suite():
    """A stale nav entry cost two full 11,400-test runs; it owns one test file."""
    import importlib.util
    import sys

    root = Path(__file__).resolve().parents[1]
    name = "mac_resolve_impacted_tests_contract_probe"
    spec = importlib.util.spec_from_file_location(
        name, root / "scripts" / "resolve-impacted-tests.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    try:
        assert module.PATH_TEST_CONTRACTS["mkdocs.yml"] == ("tests/test_docs_accessibility.py",)
        for owner in module.PATH_TEST_CONTRACTS["mkdocs.yml"]:
            assert (root / owner).is_file()
    finally:
        sys.modules.pop(name, None)


# --------------------------------------------------------------------------
# End-to-end through the real conftest hooks, in a throwaway pytest project.
#
# These prove the plumbing the plan depends on: outcomes really are recorded,
# carried-forward files really are deselected, and a previously-failing test
# really does still run and still fail.
# --------------------------------------------------------------------------


@pytest.fixture()
def mini_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    (project / "tests").mkdir(parents=True)
    real_conftest = Path(__file__).resolve().parents[1] / "conftest.py"
    (project / "conftest.py").write_text(real_conftest.read_text(encoding="utf-8"), encoding="utf-8")
    (project / "tests" / "test_green.py").write_text(
        "def test_one():\n    pass\n\n\ndef test_two():\n    pass\n", encoding="utf-8"
    )
    (project / "tests" / "test_red.py").write_text(
        "def test_broken():\n    assert False\n", encoding="utf-8"
    )
    return project


def _pytest(project: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    import os

    env = {**os.environ, **env_extra}
    for name in ("PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST", "PYTEST_XDIST_WORKER"):
        if name not in env_extra:
            env.pop(name, None)
    # The hooks only act for the session that OWNS the recording namespace.
    # Set explicitly rather than defaulted: when this test is itself run through
    # scripts/run-contract-tests.sh, the real repository's root is inherited.
    if "MAC_TEST_CHECKPOINT_ROOT" not in env_extra:
        env["MAC_TEST_CHECKPOINT_ROOT"] = str(project)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"],
        cwd=str(project),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_conftest_hooks_record_outcomes_and_then_deselect_them(mini_project, tmp_path):
    results = tmp_path / "results"
    first = _pytest(mini_project, {"MAC_TEST_CHECKPOINT_RESULTS_DIR": str(results)})
    assert first.returncode != 0, "the red test must fail on the first run"
    outcomes = tc.ingest_results(results)
    assert outcomes["tests/test_green.py::test_one"] == "passed"
    assert outcomes["tests/test_green.py::test_two"] == "passed"
    assert outcomes["tests/test_red.py::test_broken"] == "failed"

    # Now carry the green file forward and re-run. The red test must still run,
    # and must still fail: a resumed run never turns a red suite green.
    skip_file = tmp_path / "carried.txt"
    skip_file.write_text("tests/test_green.py\n", encoding="utf-8")
    second_results = tmp_path / "results2"
    second = _pytest(
        mini_project,
        {
            "MAC_TEST_CHECKPOINT_RESULTS_DIR": str(second_results),
            "MAC_TEST_CHECKPOINT_SKIP_FILE": str(skip_file),
        },
    )
    assert second.returncode != 0
    assert "2 deselected" in second.stdout
    assert tc.ingest_results(second_results) == {"tests/test_red.py::test_broken": "failed"}


def test_an_unreadable_skip_list_deselects_nothing(mini_project, tmp_path):
    """Fail OPEN: a checkpoint we cannot read must widen the run, not narrow it."""
    results = tmp_path / "results"
    completed = _pytest(
        mini_project,
        {
            "MAC_TEST_CHECKPOINT_RESULTS_DIR": str(results),
            "MAC_TEST_CHECKPOINT_SKIP_FILE": str(tmp_path / "does-not-exist.txt"),
        },
    )
    assert completed.returncode != 0
    assert "deselected" not in completed.stdout
    assert len(tc.ingest_results(results)) == 3


def test_the_hooks_are_inert_without_their_environment(mini_project, tmp_path):
    completed = _pytest(mini_project, {"MAC_TEST_CHECKPOINT_ROOT": ""})
    assert completed.returncode != 0
    assert "deselected" not in completed.stdout
    assert not (mini_project / "results").exists()


def test_a_pytest_spawned_from_inside_a_test_never_records(mini_project, tmp_path):
    """The suite shells out to pytest; those results are not the gate's results.

    On this feature's first end-to-end smoke run, a fixture project's
    deliberately-failing test was recorded into the real repository's
    checkpoint. Nothing the gate did not schedule may enter it.
    """
    results = tmp_path / "results"
    completed = _pytest(
        mini_project,
        {
            "MAC_TEST_CHECKPOINT_RESULTS_DIR": str(results),
            "MAC_TEST_CHECKPOINT_ROOT": str(mini_project),
            "PYTEST_CURRENT_TEST": "tests/test_outer.py::test_outer (call)",
        },
    )
    assert completed.returncode != 0
    assert tc.ingest_results(results) == {}


def test_a_session_rooted_elsewhere_never_records(mini_project, tmp_path):
    results = tmp_path / "results"
    completed = _pytest(
        mini_project,
        {
            "MAC_TEST_CHECKPOINT_RESULTS_DIR": str(results),
            "MAC_TEST_CHECKPOINT_ROOT": str(tmp_path / "some-other-repo"),
        },
    )
    assert completed.returncode != 0
    assert tc.ingest_results(results) == {}


def test_a_session_rooted_elsewhere_deselects_nothing(mini_project, tmp_path):
    skip_file = tmp_path / "carried.txt"
    skip_file.write_text("tests/test_green.py\n", encoding="utf-8")
    completed = _pytest(
        mini_project,
        {
            "MAC_TEST_CHECKPOINT_SKIP_FILE": str(skip_file),
            "MAC_TEST_CHECKPOINT_ROOT": str(tmp_path / "some-other-repo"),
        },
    )
    assert "deselected" not in completed.stdout


def test_a_path_that_is_both_a_test_and_source_is_charged_both_ways(repo, tmp_path):
    """plugin/test_*.py is a test file AND source code; neither may be lost."""
    directory = tmp_path / "ckpt"
    _record(repo, directory, GREEN)
    (repo / "plugin").mkdir()
    (repo / "plugin" / "test_tools.py").write_text("def test_t():\n    pass\n", encoding="utf-8")
    plan = _plan(repo, directory)
    # It is source (plugin/ + .py) that the map never saw, so it fails open.
    assert plan.mode == "full"
    assert plan.reason == "source_file_absent_from_impact_map"
