"""One hub UI: what `make run-gui` runs is what the hub serves at /ui.

The failure this file exists to prevent, observed 2026-08-20 against a live hub:

    observe/  --(build)--> src/mac/ui/console/ --> served by api.py at /ui
    ide/      --(make run-gui)--> a local dev server the hub never mounts

`make run-gui` called the unmounted tree "the canonical Fleet IDE", so an
operator who ran it to look at the hub UI got a different application, found it
did not reflect the fleet, and reported the hub UI as broken. They were right:
it was not the hub UI.

The decision (docs/adr/0025-one-hub-ui.md): the observability console built
from ``observe/`` is the product. ``ide/`` is an unshipped local prototype, not
a second canonical UI. These tests pin the wiring so the two cannot silently
drift back apart -- they check the Makefile's own dependency graph rather than
prose, because prose is what lied last time.
"""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = ROOT / "Makefile"

# The one browser surface the hub serves. Both halves of this claim are
# re-derived from source below rather than trusted: api.py must serve it and
# observe/'s Vite config must write it.
SERVED_BUNDLE = ROOT / "src" / "mac" / "ui" / "console"

# Lifecycle targets an operator reaches for when they mean "the GUI".
GUI_LIFECYCLE_TARGETS = ("run-gui", "install-gui", "build-gui", "package-gui", "install", "build")

_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)\s*[:?]?=\s*(.*)$")
_REFERENCE = re.compile(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)")


def _makefile_text() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _variables(text: str) -> dict[str, str]:
    """Simple ``NAME = value`` assignments, recursively expanded."""
    raw: dict[str, str] = {}
    for line in text.splitlines():
        if line.startswith("\t") or line.lstrip().startswith("#"):
            continue
        match = _ASSIGNMENT.match(line.strip())
        if match:
            raw.setdefault(match.group(1), match.group(2).strip())

    def expand(value: str, seen: frozenset[str] = frozenset()) -> str:
        def replace(hit: re.Match[str]) -> str:
            name = hit.group(1)
            if name in seen or name not in raw:
                return hit.group(0)
            return expand(raw[name], seen | {name})

        return _REFERENCE.sub(replace, value)

    return {name: expand(value) for name, value in raw.items()}


def _expand(value: str, variables: dict[str, str]) -> str:
    return _REFERENCE.sub(lambda hit: variables.get(hit.group(1), hit.group(0)), value)


def _rules(text: str) -> dict[str, dict[str, list[str]]]:
    """Map every target to its prerequisites and recipe lines.

    Handles multi-target rules (``ide-run ide-dev:``) and strips the ``##``
    help comment, which is documentation rather than a dependency.
    """
    variables = _variables(text)
    rules: dict[str, dict[str, list[str]]] = {}
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("\t"):
            for target in current:
                rules[target]["recipe"].append(_expand(line.strip(), variables))
            continue
        if not line or line.lstrip().startswith("#") or "=" in line.split(":")[0]:
            current = []
            continue
        if ":" not in line or line.startswith("."):
            current = []
            continue
        head, _, tail = line.partition(":")
        if head.strip().startswith("."):
            current = []
            continue
        prereqs = [
            _expand(item, variables)
            for item in tail.split("##")[0].replace("=", " ").split()
        ]
        current = [_expand(name, variables) for name in head.split()]
        for target in current:
            rules.setdefault(target, {"prereqs": [], "recipe": []})
            rules[target]["prereqs"].extend(prereqs)
    return rules


def _closure(rules: dict[str, dict[str, list[str]]], target: str) -> set[str]:
    """Every target reached from ``target`` through prerequisites."""
    seen: set[str] = set()
    pending = [target]
    while pending:
        name = pending.pop()
        if name in seen or name not in rules:
            continue
        seen.add(name)
        pending.extend(rules[name]["prereqs"])
    return seen


def _recipes(rules: dict[str, dict[str, list[str]]], targets: set[str]) -> str:
    return "\n".join(line for name in sorted(targets) for line in rules[name]["recipe"])


def test_run_gui_runs_the_tree_the_hub_serves() -> None:
    """`make run-gui` must reach observe/ and must not reach ide/.

    This is the whole bug in one assertion: run-gui used to resolve to
    ide-run, a tree api.py has never mounted.
    """
    rules = _rules(_makefile_text())

    assert "run-gui" in rules, "the Makefile no longer defines run-gui"
    reached = _closure(rules, "run-gui")
    recipes = _recipes(rules, reached)

    assert "observe" in recipes, (
        "make run-gui no longer runs the observe/ tree the hub serves at /ui; "
        f"reached targets: {sorted(reached)}"
    )
    assert "ide-run" not in reached and "ide-dev" not in reached, (
        "make run-gui runs the unshipped Fleet IDE prototype again -- that is "
        "the exact drift this test exists to catch"
    )
    assert not re.search(r"\bide/", recipes), (
        f"make run-gui touches ide/, which the hub does not serve:\n{recipes}"
    )


def test_gui_lifecycle_targets_all_point_at_the_served_tree() -> None:
    """install/build/package/run of "the GUI" must mean one product."""
    rules = _rules(_makefile_text())

    for target in GUI_LIFECYCLE_TARGETS:
        assert target in rules, f"the Makefile no longer defines {target}"
        recipes = _recipes(rules, _closure(rules, target))
        assert not re.search(r"\bide/", recipes), (
            f"make {target} builds or runs ide/, which is not the UI the hub "
            f"serves:\n{recipes}"
        )

    build_gui = _recipes(rules, {"build-gui"})
    assert "cd observe" in build_gui, (
        f"build-gui no longer builds the hub UI from observe/:\n{build_gui}"
    )


def test_vite_writes_exactly_the_bundle_api_py_serves() -> None:
    """The build output path and the served path are re-derived, not asserted.

    If either side moves without the other, the served bundle and the built
    bundle stop being the same tree and this fails.
    """
    api = (ROOT / "src" / "mac" / "api.py").read_text(encoding="utf-8")
    vite = (ROOT / "observe" / "vite.config.ts").read_text(encoding="utf-8")

    # api.py: StaticFiles over src/mac/ui, and every /ui route returns
    # <that dir>/console/index.html.
    assert 'ui_dir = Path(__file__).with_name("ui")' in api
    assert 'app.mount("/ui/assets", StaticFiles(directory=str(ui_dir))' in api
    assert 'FileResponse(ui_dir / "console" / "index.html")' in api
    served = ROOT / "src" / "mac" / "ui" / "console"
    assert served == SERVED_BUNDLE

    out_dir = re.search(r'outDir:\s*"([^"]+)"', vite)
    assert out_dir, "observe/vite.config.ts no longer declares a build.outDir"
    built = (ROOT / "observe" / out_dir.group(1)).resolve()

    assert built == served.resolve(), (
        f"observe/ builds into {built} but the hub serves {served}; "
        "make run-gui and /ui would no longer be the same bundle"
    )

    # The committed bundle really is that build's output.
    assert (served / "index.html").is_file()
    assert (served / "console.js").is_file()
    base = re.search(r'base:\s*"([^"]+)"', vite)
    assert base and base.group(1) == "/ui/assets/console/", (
        "the console's asset base must match the hub's /ui/assets mount"
    )


def test_the_ide_prototype_is_not_labelled_canonical() -> None:
    """Two UIs is survivable. Two UIs where the unserved one is called
    "canonical" is what sent an operator to the wrong application."""
    text = _makefile_text()

    offenders = [
        line
        for line in text.splitlines()
        if "canonical" in line.lower() and re.search(r"\bide\b|\bIDE\b", line)
    ]
    assert not offenders, (
        "the Makefile calls the Fleet IDE canonical while the hub serves the "
        "console; make api.py serve ide/ before restoring that word:\n"
        + "\n".join(offenders)
    )
