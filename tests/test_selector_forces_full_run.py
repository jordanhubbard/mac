"""A change to the test selector must force a full run.

`test-policy.toml` was already in `global_full_paths`; the CODE it configures
was not. So a change to the selection machinery was scoped by the changed
selection machinery. Observed on PR #408 (run 32104798882), which modified
`scripts/select-sanity-tests.py` and `src/mac/test_checkpoint.py`:

    sanity selection: focused (impact_hybrid_scope)
      provenance: 2 from the change, 10 always_run guards
    collected 750 items
    746 passed, 4 skipped in 68.70s

750 of ~11,400 tests, 1m44s instead of ~50min. The change happened to be
correct. But `tests/test_select_sanity_tests.py` -- the file whose two failures
prompted that fix -- was selected ZERO times, so that run could not have
contradicted the fix even if it had been wrong in exactly the original way.

This is the trusted-computing-base argument. Every other gate in this
repository is only as strong as the component that decides which tests run;
that component alone cannot be permitted to narrow its own verification. It is
also the reason the cheaper option was rejected: adding the selector's test
files to `always_run` closes the one hole we happened to notice, and leaves the
general property ("you cannot shrink your own gate") unenforced.
"""

from __future__ import annotations

import importlib.util
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Changing any of these changes WHICH TESTS RUN.
SELECTOR_FILES = (
    "test-policy.toml",
    "scripts/select-sanity-tests.py",
    "scripts/resolve-impacted-tests.py",
    "scripts/build-test-impact-map.py",
    "src/mac/test_checkpoint.py",
)


def _policy() -> dict:
    with (ROOT / "test-policy.toml").open("rb") as stream:
        return tomllib.load(stream)["selection"]


@pytest.mark.parametrize("path", SELECTOR_FILES)
def test_each_selector_file_forces_a_full_run(path):
    assert path in _policy()["global_full_paths"], (
        "%s decides which tests CI runs, but changing it does not force a full "
        "run -- so the changed selector chooses the scope that verifies itself" % path
    )


@pytest.mark.parametrize("path", SELECTOR_FILES)
def test_each_selector_file_exists(path):
    """A renamed selector silently drops out of global_full_paths.

    `global_full_paths` matches on exact repository-relative path, so a rename
    turns the entry into a no-op with no error anywhere -- the same failure
    shape as a stranded impact-map node id.
    """
    assert (ROOT / path).exists(), (
        "%s is listed in global_full_paths but does not exist; the entry is "
        "now dead and the file it was meant to guard is unguarded" % path
    )


def _selector():
    name = "mac_select_sanity_tests_globalcheck"
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / "select-sanity-tests.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", SELECTOR_FILES)
def test_the_resolver_actually_escalates_each_one(path):
    """End to end, not just the config: the change must produce mode=full."""
    result = _selector().select([path], codegraph=lambda _s, _r: ([], None))

    assert result["mode"] == "full"
    assert result["reason"] == "global_infrastructure_changed"
    assert path in result["global_files"]


def test_an_ordinary_source_change_is_still_focused():
    """The point is a narrow escalation, not a return to over-escalation.

    The previous selector escalated on every scripts/ and pyproject edit, which
    is exactly what impact selection exists to remove.
    """
    result = _selector().select(
        ["src/mac/services.py"],
        codegraph=lambda _s, _r: (["tests/test_services.py"], None),
    )

    assert result["mode"] == "focused"


@pytest.mark.parametrize(
    "path",
    ["pyproject.toml", "scripts/run-contract-tests.sh", "deploy/codex-runner/build.sh"],
)
def test_the_old_broad_prefixes_stay_out(path):
    assert path not in _policy()["global_full_paths"]
