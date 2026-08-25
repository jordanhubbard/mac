"""The UI `make run-gui` runs and the UI the hub serves must be one tree.

They were two. `observe/` built into ``src/mac/ui/console/`` and the hub served
that at ``/ui``; ``run-gui`` ran ``ide/``, which no hub has ever mounted — and
the Makefile called *that* one "canonical". An operator who ran ``make run-gui``
to look at the hub UI got a different application and reported it as broken,
correctly. ADR 0025 makes the console the hub UI and names ``ide/`` a prototype.

This test walks the real chain rather than trusting prose:

    observe/vite.config.ts  --outDir-->  src/mac/ui/console
                                              |
                        api.py serves ui_dir/"console"/index.html at /ui
                                              |
                     make -n run-gui  runs that same source directory

If any link stops naming the same tree — or if ``run-gui`` reaches back into
``ide/`` — this fails. That is the whole point: the drift was silent, every test
passed, and only a live ``curl`` against the hub exposed it.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
OBSERVE_DIR = ROOT / "observe"
SERVED_BUNDLE = ROOT / "src" / "mac" / "ui" / "console"

pytestmark = pytest.mark.ui


def _make(*args: str) -> str:
    """Run make in --dry-run mode: it prints the recipe, it does not run it."""
    result = subprocess.run(
        ["make", "-n", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def _title(html: Path) -> str:
    match = re.search(r"<title>(.*?)</title>", html.read_text(encoding="utf-8"), re.S)
    assert match, "%s has no <title>" % html
    return match.group(1).strip()


def test_the_console_source_builds_into_the_directory_the_hub_serves() -> None:
    """observe/'s build output is the hub's static tree, not a sibling of it."""
    vite = (OBSERVE_DIR / "vite.config.ts").read_text(encoding="utf-8")
    match = re.search(r"""outDir:\s*["']([^"']+)["']""", vite)
    assert match, "observe/vite.config.ts declares no build.outDir"

    out_dir = (OBSERVE_DIR / match.group(1)).resolve()
    assert out_dir == SERVED_BUNDLE.resolve(), "observe/ builds into %s but the hub serves %s" % (
        out_dir,
        SERVED_BUNDLE,
    )
    assert (SERVED_BUNDLE / "index.html").is_file(), "the committed bundle is missing"


def test_the_hub_serves_that_bundle_at_ui() -> None:
    """/ui returns the console shell out of the committed bundle."""
    import mac.api

    api_source = Path(mac.api.__file__).read_text(encoding="utf-8")
    assert 'ui_dir = Path(__file__).with_name("ui")' in api_source
    assert 'return FileResponse(ui_dir / "console" / "index.html")' in api_source

    served = Path(mac.api.__file__).with_name("ui") / "console" / "index.html"
    assert served.is_file()
    # Same document, byte for byte -- the installed package and the repository
    # bundle are one file, so "what the hub serves" is not a copy that can rot.
    assert served.read_bytes() == (SERVED_BUNDLE / "index.html").read_bytes()


def test_run_gui_runs_the_served_tree_and_not_the_prototype() -> None:
    """`make run-gui` drives observe/, the directory the hub's bundle comes from."""
    recipe = _make("run-gui")

    assert "cd observe" in recipe, recipe
    assert "src/mac/ui/console" in recipe, recipe

    # The regression itself: run-gui must not reach into the unshipped prototype.
    assert "mac.ide_launcher" not in recipe, recipe
    assert not re.search(r"\bide/|\bcd ide\b|IDE_PORT", recipe), recipe


def test_the_generic_gui_targets_build_and_package_the_served_tree() -> None:
    """install/build/package -gui produce the hub UI, not the prototype."""
    for target in ("build-gui", "install-gui", "package-gui"):
        recipe = _make(target)
        assert "cd observe" in recipe, "%s: %s" % (target, recipe)
        assert "cd ide" not in recipe, "%s: %s" % (target, recipe)

    assert "src/mac/ui/console" in _make("package-gui")
    # `make build` is the front-door build; it must not silently build ide/.
    assert "cd ide" not in _make("build")


def test_the_prototype_keeps_its_own_named_targets() -> None:
    """ide/ stays runnable -- under ide-* names that promise nothing else."""
    for target in ("ide-build", "ide-package", "ide-run"):
        recipe = _make(target)
        assert "ide" in recipe, "%s: %s" % (target, recipe)
        assert "cd observe" not in recipe, "%s: %s" % (target, recipe)


def test_the_makefile_does_not_call_the_unserved_ui_canonical() -> None:
    """The word that caused this: nothing may label ide/ the canonical UI."""
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")

    assert "run-gui: ide-run" not in makefile
    assert not re.search(r"canonical\s+Fleet\s+IDE", makefile), (
        "the Makefile calls the Fleet IDE canonical; the hub does not serve it"
    )

    help_text = subprocess.run(
        ["make", "help"], cwd=ROOT, text=True, capture_output=True, check=False
    ).stdout
    assert "/ui" in help_text, "make help should name where the hub UI actually is"


def test_the_shipped_shell_is_the_console_not_the_ide() -> None:
    """A title is a cheap, human-checkable identity for 'which app is this'."""
    served_title = _title(SERVED_BUNDLE / "index.html")
    source_title = _title(OBSERVE_DIR / "index.html")

    assert served_title == source_title
    assert "observability" in served_title.lower(), served_title
    assert _title(ROOT / "ide" / "index.html") != served_title
