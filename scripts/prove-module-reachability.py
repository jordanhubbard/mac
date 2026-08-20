#!/usr/bin/env python3
"""Prove which top-level symbols of a module are unreachable from production.

Written for the Hermes garbage-collection (ADR 0025), which rests on a claim
that has to be *proved* rather than asserted: "these modules are dead". They
were not, and the only way to tell the live plumbing from the dead runtime was
to compute it.

The gate at ``scripts/dead-code-check.sh`` runs vulture at >=90% confidence,
which is deliberately tuned to catch unreachable statements and unused imports
without drowning the push in false positives. It does not catch the case this
prover exists for: a module-level function nobody calls, whose only "user" is
its own module's other dead functions. Vulture scores that at 60%, where the
false-positive rate is too high to gate on.

So this prover is narrower and stricter than vulture, in exchange for being
trustworthy on one question:

    Starting from the production entry points, which top-level symbols of the
    named modules are never reached?

Reachability is computed as a fixed point over three reference sources:

1. **Cross-module Python references.** Every ``.py`` file under ``src/`` other
   than the module itself, parsed with ``ast``. An ``import``, a qualified
   ``mac.mod.sym``, or a bare ``sym`` after ``from mac.mod import sym`` all
   count.
2. **Non-Python references.** ``deploy/``, ``scripts/`` and ``pyproject.toml``
   are scanned textually, because the fleet installers embed Python in shell
   heredocs (``python -m mac.hermes_runtime``, ``from mac.hermes_startup import
   ...``) and ``[project.scripts]`` names entry points as strings. A symbol
   named there is live even though no ``.py`` file mentions it.
3. **Intra-module references.** Within the module, a symbol's body is walked
   for names; those edges propagate reachability from seeds inward.

Tests are *not* a reference source. A symbol whose only caller is its own test
is dead code plus a dead test, and reporting it as live would defeat the point.
``--include-tests`` overrides this when you want to see the difference.

Exit status is 0 when the unreachable set matches ``--expect`` (default: empty),
so this can be wired into a gate once a module has been cleaned.

Usage::

    scripts/prove-module-reachability.py src/mac/hermes_runtime.py
    scripts/prove-module-reachability.py --json src/mac/hermes_*.py
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Set

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT = REPO_ROOT / "src"
TEXT_SCAN_ROOTS = ("deploy", "scripts")
TEXT_SCAN_FILES = ("pyproject.toml", "Makefile")
TEXT_SCAN_SUFFIXES = {".sh", ".py", ".toml", ".yaml", ".yml", ".plist", ".service", ""}

# Symbols that are reachable by construction rather than by reference: module
# entry points invoked as ``python -m mac.<mod>``, and dunder protocol names.
ALWAYS_REACHABLE = {"main", "_main", "__all__"}


def _module_name(path: Path) -> str:
    """``src/mac/hermes_runtime.py`` -> ``mac.hermes_runtime``."""
    rel = path.resolve().relative_to(SOURCE_ROOT)
    return ".".join(rel.with_suffix("").parts)


def _top_level_symbols(tree: ast.Module) -> Dict[str, ast.AST]:
    """Map every top-level definition/binding to the node that defines it."""
    symbols: Dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols[node.target.id] = node
    return symbols


def _names_in(node: ast.AST) -> Set[str]:
    """Every identifier a node could be referring to, attributes included.

    Attribute access is flattened to its terminal name (``mod.sym`` -> ``sym``)
    because the prover answers "is this symbol named anywhere", not "is this
    exact binding used". Over-approximating here is the safe direction: it can
    only report a dead symbol as live, never a live symbol as dead.
    """
    names: Set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute):
            names.add(child.attr)
        elif isinstance(child, (ast.ImportFrom, ast.Import)):
            for alias in child.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            # ``getattr(mod, "sym")`` and FastAPI/argparse dispatch tables refer
            # to symbols by string. Cheap to honour, and again over-approximates.
            names.add(child.value)
    return names


def _external_python_references(target_module: str) -> Set[str]:
    """Names mentioned by any source file other than the target module."""
    referenced: Set[str] = set()
    for path in sorted(SOURCE_ROOT.rglob("*.py")):
        if _module_name(path) == target_module:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        referenced |= _names_in(tree)
    return referenced


def _text_scan_paths() -> Iterable[Path]:
    for name in TEXT_SCAN_FILES:
        candidate = REPO_ROOT / name
        if candidate.is_file():
            yield candidate
    for root in TEXT_SCAN_ROOTS:
        base = REPO_ROOT / root
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.suffix in TEXT_SCAN_SUFFIXES:
                yield path


def _scan_text(paths: Iterable[Path], symbols: Iterable[str], *, skip: Path | None = None) -> Set[str]:
    """Which of ``symbols`` are named as whole words in any of ``paths``.

    Whole words, not substrings: a plain ``symbol in blob`` reports ``orphan``
    as referenced because the file mentions ``tested_orphan``. That failure mode
    is one-directional and quiet — it can only turn dead symbols into live ones,
    which is precisely the wrong direction for a prover whose output authorises
    a delete.
    """
    wanted = set(symbols)
    if not wanted:
        return set()
    pattern = re.compile(r"\b(" + "|".join(re.escape(s) for s in sorted(wanted)) + r")\b")
    skip = skip.resolve() if skip else None
    found: Set[str] = set()
    for path in paths:
        if skip is not None and path.resolve() == skip:
            continue
        try:
            blob = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        found |= set(pattern.findall(blob))
        if found == wanted:
            break
    return found


def _text_references(symbols: Iterable[str], skip: Path) -> Set[str]:
    """Symbols named anywhere in the shell/config surface.

    The fleet installers are the reason this exists: ``fleet-node-install.sh``
    carries multi-hundred-line Python heredocs that import from these modules,
    and a prover that only reads ``.py`` files would call that plumbing dead.
    """
    return _scan_text(_text_scan_paths(), symbols, skip=skip)


def _test_references(symbols: Iterable[str]) -> Set[str]:
    tests_root = REPO_ROOT / "tests"
    if not tests_root.is_dir():
        return set()
    return _scan_text(sorted(tests_root.rglob("*.py")), symbols)


def _intra_module_edges(symbols: Dict[str, ast.AST]) -> Dict[str, Set[str]]:
    """symbol -> the other top-level symbols its body names."""
    own = set(symbols)
    return {name: _names_in(node) & own - {name} for name, node in symbols.items()}


def analyse(path: Path, *, include_tests: bool = False) -> Dict[str, object]:
    module = _module_name(path)
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    symbols = _top_level_symbols(tree)
    edges = _intra_module_edges(symbols)

    external = _external_python_references(module) & set(symbols)
    textual = _text_references(symbols, skip=path)
    seeds = external | textual | (ALWAYS_REACHABLE & set(symbols))
    if include_tests:
        seeds |= _test_references(symbols)

    # Fixed point: a symbol is reachable if it is a seed or is named by the
    # body of a reachable symbol.
    reachable = set(seeds)
    frontier = list(reachable)
    while frontier:
        for callee in edges.get(frontier.pop(), ()):
            if callee not in reachable:
                reachable.add(callee)
                frontier.append(callee)

    unreachable = sorted(set(symbols) - reachable)
    return {
        "module": module,
        "path": str(path.resolve().relative_to(REPO_ROOT)),
        "symbols": len(symbols),
        "reachable": len(reachable),
        "seeds": {
            "cross_module": sorted(external),
            "non_python": sorted(textual - external),
            "tests": sorted(_test_references(symbols) - external - textual)
            if include_tests
            else [],
        },
        "unreachable": unreachable,
        "unreachable_only_tested": sorted(
            set(unreachable) & _test_references(symbols)
        )
        if not include_tests
        else [],
    }


def _parse_args(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("modules", nargs="+", type=Path)
    parser.add_argument(
        "--include-tests",
        action="store_true",
        help="count test files as a reference source (off by default: a symbol "
        "whose only caller is its own test is dead code plus a dead test)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--expect",
        default="",
        help="comma-separated symbols permitted to be unreachable; exit 1 on any other",
    )
    return parser.parse_args(argv)


def main(argv: List[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    expected = {s.strip() for s in args.expect.split(",") if s.strip()}

    reports = [analyse(p, include_tests=args.include_tests) for p in args.modules]

    if args.json:
        print(json.dumps(reports, indent=2, sort_keys=True))
    else:
        for report in reports:
            unreachable = report["unreachable"]
            print(f"{report['path']}: {report['reachable']}/{report['symbols']} reachable")
            for symbol in unreachable:
                tag = (
                    " (referenced only by tests)"
                    if symbol in report["unreachable_only_tested"]
                    else ""
                )
                print(f"    UNREACHABLE {symbol}{tag}")

    surprises = sorted({s for r in reports for s in r["unreachable"]} - expected)
    if surprises:
        print(
            "prove-module-reachability: unreachable symbols not in --expect: "
            + ", ".join(surprises),
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
