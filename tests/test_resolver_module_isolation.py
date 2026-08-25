"""The test selector must not inherit another repository's policy.

`scripts/resolve-impacted-tests.py` captures its repository root at import and
reads `test-policy.toml` and the impact map relative to it. Both loaders cached
that module under ONE fixed `sys.modules` key, `mac_resolve_impacted_tests`, so
the first caller in a process decided the answers for every later one.

`tests/test_test_checkpoint.py` builds a synthetic repo whose policy declares
`Makefile` global and `always_run = ["tests/test_guard.py"]`, loads the resolver
against it, and left it installed. Every later caller in that pytest worker then
resolved against a temp directory that had already been deleted:

    tests/test_select_sanity_tests.py::test_opaque_non_code_forces_full
        assert 'global_infrastructure_changed' == 'unmappable_non_code_change'
    tests/test_select_sanity_tests.py::test_source_change_uses_codegraph_and_canaries
        assert '...public_contract.py' in ['tests/test_guard.py', 'tests/test_mapped.py']

Each file passes alone. It only fails when `-n 8` puts them in one worker, which
is why it sat until an unrelated new test file shifted xdist's distribution.

THE REASON THIS IS NOT MERELY TWO RED TESTS. The contaminated module is the one
that CHOOSES WHICH TESTS CI RUNS. Here a wrong policy made assertions fail, which
is the lucky outcome. The same wrong policy during a real selection UNDER-SELECTS:
fewer tests run, the gate reports success, and nothing anywhere says so.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from mac import test_checkpoint as tc

ROOT = Path(__file__).resolve().parents[1]


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def foreign_repo(tmp_path: Path) -> Path:
    """A checkout whose policy disagrees with the real one on every point."""
    root = tmp_path / "other-repo"
    (root / "scripts").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "scripts" / "resolve-impacted-tests.py").write_text(
        (ROOT / "scripts" / "resolve-impacted-tests.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (root / "test-policy.toml").write_text(
        '[selection]\nglobal_full_paths = ["Makefile"]\nalways_run = ["tests/test_guard.py"]\n',
        encoding="utf-8",
    )
    _git(root, "init", "-q", "-b", "main")
    return root


def _load_selector():
    """A fresh copy of scripts/select-sanity-tests.py, as CI invokes it."""
    name = "mac_select_sanity_tests_isolation"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "select-sanity-tests.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_a_foreign_repo_does_not_capture_the_shared_module_key(foreign_repo):
    """The exact sequence that broke CI, reduced to two calls."""
    foreign = tc.load_resolver(foreign_repo)
    assert foreign is not None

    ours = tc.load_resolver(ROOT)
    assert ours is not None

    assert ours is not foreign, (
        "loading the resolver against a temp repo returned that same module "
        "for the real checkout; every policy question is now answered by a "
        "directory that pytest is about to delete"
    )
    assert "Makefile" not in ours.load_policy().global_full_paths
    assert "Makefile" in foreign.load_policy(foreign_repo / "test-policy.toml").global_full_paths


def test_the_selector_reads_its_own_policy_after_a_foreign_load(foreign_repo):
    """End to end: the failure CI actually reported."""
    tc.load_resolver(foreign_repo)

    selector = _load_selector()
    result = selector.select(["Makefile"], codegraph=lambda _s, _r: ([], None))

    assert result["reason"] == "unmappable_non_code_change", (
        "Makefile is global infrastructure ONLY in the synthetic policy; the "
        "selector is reading a foreign repository's test-policy.toml"
    )


def test_canaries_come_from_the_real_policy_after_a_foreign_load(foreign_repo):
    tc.load_resolver(foreign_repo)

    selector = _load_selector()
    resolver = selector._resolver()
    always_run = resolver.load_policy().always_run

    assert "tests/test_guard.py" not in always_run, (
        "the canary list is the synthetic one; a real selection would run the "
        "wrong guards and skip the public-contract canary"
    )
    assert "tests/test_control_plane_public_contract.py" in always_run


def test_the_module_key_distinguishes_roots(tmp_path):
    a = tc.resolver_module_name(tmp_path / "a")
    b = tc.resolver_module_name(tmp_path / "b")

    assert a != b
    assert tc.resolver_module_name(tmp_path / "a") == a, "must be deterministic"


def test_the_same_root_is_still_cached(foreign_repo):
    """Keying by root must not turn every call into a fresh exec_module."""
    first = tc.load_resolver(foreign_repo)

    assert tc.load_resolver(foreign_repo) is first


def test_an_unreadable_root_still_returns_none(tmp_path):
    assert tc.load_resolver(tmp_path / "nonexistent") is None
    assert not [
        name
        for name in sys.modules
        if name.startswith(tc._RESOLVER_NAME) and sys.modules[name] is None
    ], "a failed load must not leave a placeholder behind"
