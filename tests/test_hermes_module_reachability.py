"""The Hermes modules are live plumbing, and this is what keeps saying so.

ADR 0025 rests on one measurement: 190 of 191 top-level symbols across the five
``hermes_*`` modules are reached from a production entry point, so the modules
are a rename problem rather than a delete problem. A measurement written into a
document is true on the day it is written and drifts silently afterwards. These
tests re-run it.

They cover both directions:

* the prover itself is correct on a synthetic module with a known answer, and
* the five real modules have no unreachable top-level symbols.

The second is the gate. If someone adds a function nobody calls, it names the
function instead of letting it join the pile that made "just delete the Hermes
runtime" look reasonable in the first place.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
PROVER = ROOT / "scripts" / "prove-module-reachability.py"

HERMES_MODULES = (
    "hermes_adapter.py",
    "hermes_chat_config.py",
    "hermes_config_surface.py",
    "hermes_runtime.py",
    "hermes_startup.py",
)


def _load_prover():
    spec = importlib.util.spec_from_file_location("mac_prove_module_reachability", PROVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def prover():
    return _load_prover()


def test_prover_separates_reachable_from_unreachable(prover, tmp_path, monkeypatch):
    """A synthetic module with a known answer, so the gate below means something.

    ``entry`` is seeded from outside; ``helper`` is reached only through it;
    ``orphan`` is reached by nobody; ``tested_orphan`` is reached only by a
    test, which does not count as reachable but is reported separately so the
    reader knows to delete the test too.
    """
    src = tmp_path / "src"
    pkg = src / "mac"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "target.py").write_text(
        "def helper():\n"
        "    return 1\n"
        "\n"
        "def entry():\n"
        "    return helper()\n"
        "\n"
        "def orphan():\n"
        "    return 2\n"
        "\n"
        "def tested_orphan():\n"
        "    return 3\n",
        encoding="utf-8",
    )
    (pkg / "caller.py").write_text(
        "from mac.target import entry\n\n\ndef go():\n    return entry()\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_target.py").write_text(
        "from mac.target import tested_orphan\n\n\n"
        "def test_it():\n    assert tested_orphan() == 3\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(prover, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(prover, "SOURCE_ROOT", src)

    report = prover.analyse(pkg / "target.py")

    assert report["module"] == "mac.target"
    assert report["unreachable"] == ["orphan", "tested_orphan"]
    assert report["unreachable_only_tested"] == ["tested_orphan"]
    assert "entry" in report["seeds"]["cross_module"]

    # Counting tests as a reference source pulls tested_orphan back in, which is
    # exactly why the default does not.
    with_tests = prover.analyse(pkg / "target.py", include_tests=True)
    assert with_tests["unreachable"] == ["orphan"]


def test_prover_counts_shell_heredoc_references(prover, tmp_path, monkeypatch):
    """``fleet-node-install.sh`` imports these modules from Python-in-shell.

    A prover that only read ``.py`` files would call that plumbing dead and
    invite exactly the delete this ADR exists to prevent.
    """
    src = tmp_path / "src"
    pkg = src / "mac"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "target.py").write_text(
        "def only_called_from_shell():\n    return 1\n", encoding="utf-8"
    )
    deploy = tmp_path / "deploy"
    deploy.mkdir()
    (deploy / "fleet-node-install.sh").write_text(
        '"$VENV/bin/python" - <<\'PY\'\n'
        "from mac.target import only_called_from_shell\n"
        "only_called_from_shell()\n"
        "PY\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(prover, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(prover, "SOURCE_ROOT", src)

    report = prover.analyse(pkg / "target.py")
    assert report["unreachable"] == []
    assert report["seeds"]["non_python"] == ["only_called_from_shell"]


@pytest.mark.parametrize("module", HERMES_MODULES)
def test_hermes_module_has_no_unreachable_symbols(prover, module):
    """ADR 0025's central measurement, re-run.

    Failure means one of two things, and the fix differs:

    * a new symbol nobody calls — delete it, or wire it up;
    * a symbol reached only in a way the prover cannot see — add the reference
      source to the prover rather than suppressing the finding here.
    """
    report = prover.analyse(ROOT / "src" / "mac" / module)
    assert report["unreachable"] == [], (
        f"{module}: unreachable top-level symbols {report['unreachable']}. "
        "See docs/adr/0025-hermes-is-a-persona-name-not-a-runtime.md — genuine "
        "dead code must be deleted, not allowlisted."
    )
    assert report["symbols"] > 0


def test_the_deleted_runtime_stays_deleted():
    """`update_fleet_hermes_surface` was the whole of the unreachable runtime.

    It wrote a fleet-level Hermes surface into the registry and had no caller
    outside its own coverage test. It is named here so a future reader who finds
    the removal in the history does not restore it as an oversight.
    """
    from mac import hermes_config_surface

    assert not hasattr(hermes_config_surface, "update_fleet_hermes_surface")
