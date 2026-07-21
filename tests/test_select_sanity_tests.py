"""Tests for scripts/select-sanity-tests.py, now a thin resolver adapter.

The heavy safety matrix lives in tests/test_resolve_impacted_tests.py. Here we
verify the adapter delegates correctly, preserves the ``mac.sanity_selection.v1``
contract consumed by run-sanity-tests.sh, and no longer categorically escalates
the prefixes the old broad-path list forced to a full run.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "select-sanity-tests.py"


def _load_module():
    name = "mac_select_sanity_tests"
    spec = importlib.util.spec_from_file_location(name, SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def selector():
    return _load_module()


@pytest.fixture()
def resolver(selector, monkeypatch):
    mod = selector._resolver()
    # Neutralise on-disk existence so synthetic paths survive; each test injects
    # its own CodeGraph so nothing shells out.
    monkeypatch.setattr(mod, "_existing", lambda paths, repo_root: list(paths))
    return mod


def _no_cg(_source, _root):
    return [], None


def test_empty_scope_is_fail_closed_full(selector, resolver):
    result = selector.select([], codegraph=_no_cg)
    assert result["schema"] == selector.SCHEMA
    assert result["mode"] == "full"
    assert result["reason"] == "no_changed_file_scope"


def test_global_infrastructure_path_forces_full(selector, resolver):
    result = selector.select(["test-policy.toml", "src/mac/services.py"], codegraph=_no_cg)
    assert result["mode"] == "full"
    assert result["reason"] == "global_infrastructure_changed"
    assert "test-policy.toml" in result["global_files"]


def test_opaque_non_code_forces_full(selector, resolver):
    result = selector.select(["Makefile"], codegraph=_no_cg)
    assert result["mode"] == "full"
    assert result["reason"] == "unmappable_non_code_change"


def test_documentation_only_selects_no_tests(selector, resolver):
    result = selector.select(["docs/readme.md"], codegraph=_no_cg)
    assert result["mode"] == "focused"
    assert result["reason"] == "non_code_change"
    assert result["tests"] == []


def test_fault_replay_test_file_no_longer_forces_full(selector, resolver):
    # Old behaviour: tests/fault_replay/ was a BROAD_PREFIX -> full. Now a test
    # file there is simply selected directly.
    result = selector.select(["tests/fault_replay/test_replay.py"], codegraph=_no_cg)
    assert result["mode"] == "focused"
    assert "tests/fault_replay/test_replay.py" in result["tests"]


def test_source_change_uses_codegraph_and_canaries(selector, resolver):
    result = selector.select(
        ["src/mac/thing.py"],
        codegraph=lambda src, root: (["tests/test_mapped.py"], None),
    )
    assert result["mode"] == "focused"
    assert result["reason"] == "impact_hybrid_scope"
    assert "tests/test_mapped.py" in result["tests"]
    # Canaries from [selection].always_run ride along on a real code change.
    assert "tests/test_control_plane_public_contract.py" in result["tests"]


def test_source_change_without_map_or_codegraph_is_full(selector, resolver):
    result = selector.select(["src/mac/brand_new_module.py"], codegraph=_no_cg)
    assert result["mode"] == "full"


def test_resolver_no_longer_broadly_escalates_prefixes(selector, resolver):
    policy = resolver.load_policy()
    for prefix_path in (
        "scripts/run-contract-tests.sh",
        "deploy/codex-runner/build.sh",
        "tests/fault_replay/test_replay.py",
        "pyproject.toml",
    ):
        assert prefix_path not in policy.global_full_paths


def test_main_json_output(selector, capsys):
    rc = selector.main(["--changed-file", "docs/readme.md"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["schema"] == selector.SCHEMA
    assert document["changed_files"] == ["docs/readme.md"]
    assert document["mode"] == "focused"


def test_main_tests_only_output(selector, monkeypatch, capsys):
    mod = selector._resolver()
    monkeypatch.setattr(
        mod,
        "select_from_git",
        lambda **kwargs: {"tests": ["tests/test_a.py", "tests/test_b.py"]},
    )
    rc = selector.main(["--changed-file", "src/mac/thing.py", "--tests-only"])
    assert rc == 0
    assert capsys.readouterr().out.splitlines() == ["tests/test_a.py", "tests/test_b.py"]


def test_main_handles_selection_error(selector, monkeypatch, capsys):
    mod = selector._resolver()

    def _boom(base, repo_root):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(mod, "git_changed_files", _boom)
    rc = selector.main([])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["mode"] == "full"
    assert document["reason"] == "selection_error"
    assert "git exploded" in document["error"]
