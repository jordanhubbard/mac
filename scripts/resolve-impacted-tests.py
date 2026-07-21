#!/usr/bin/env python3
"""Resolve the impact-based test scope for a change set.

A test is selected only when the code it actually exercises changed. The engine
is hybrid, union, and fail-closed:

1. Dynamic per-test coverage map (scripts/build-test-impact-map.py): for a fresh
   map, select tests whose executed lines intersect the changed lines of a
   source file (line-level), falling back to every test that touched the file
   when only additions are present.
2. CodeGraph static reachability: union in `codegraph affected` so a change that
   makes a test reach NEW code is still covered even though the map (built at the
   base revision) could not know about it.
3. Full-suite fallback: any changed file that neither layer can safely map
   (stale/missing map entry plus unusable CodeGraph, an opaque infra file, or a
   globally invalidating file) forces a full run.

`resolve()` is a pure function (no git, no IO) so the safety matrix is fully
unit-tested; `select_from_git()` gathers the git diff, map, and CodeGraph and
delegates to it. Output is the existing ``mac.sanity_selection.v1`` document so
it drops straight into scripts/run-sanity-tests.sh.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "mac.sanity_selection.v1"
CODE_EXTS = {".py", ".js", ".ts", ".tsx"}
DOC_SUFFIXES = {".md", ".rst", ".txt", ".adoc", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".webp"}
DEFAULT_POLICY = ROOT / "test-policy.toml"
DEFAULT_MAP = ROOT / "src" / "mac" / "data" / "test_impact_map.json"
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")


@dataclass
class SelectionPolicy:
    """The `[selection]` contract from test-policy.toml."""

    global_full_paths: frozenset[str] = frozenset()
    always_run: tuple[str, ...] = ()
    map_path: Path = DEFAULT_MAP


def load_policy(policy_path: Path = DEFAULT_POLICY) -> SelectionPolicy:
    try:
        with policy_path.open("rb") as stream:
            document = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError):
        return SelectionPolicy()
    section = document.get("selection", {}) if isinstance(document, dict) else {}
    map_path = section.get("impact_map")
    return SelectionPolicy(
        global_full_paths=frozenset(section.get("global_full_paths", [])),
        always_run=tuple(section.get("always_run", [])),
        map_path=(ROOT / map_path) if map_path else DEFAULT_MAP,
    )


def _is_documentation(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return path.startswith("docs/") or suffix in DOC_SUFFIXES


def _is_test_file(path: str) -> bool:
    return (
        path.startswith(("tests/", "plugin/"))
        and path.endswith(".py")
        and Path(path).name.startswith("test_")
    )


def _is_source_code(path: str) -> bool:
    return path.startswith(("src/", "plugin/")) and Path(path).suffix in CODE_EXTS


def _existing(paths: Iterable[str], repo_root: Path) -> list[str]:
    return [path for path in paths if (repo_root / path).is_file()]


def _full(reason: str, changed: list[str], **extra: object) -> dict[str, object]:
    return {"schema": SCHEMA, "mode": "full", "reason": reason, "changed_files": changed, "tests": [], **extra}


def resolve(
    changed_files: Iterable[str],
    changed_base_lines: dict[str, set[int]],
    *,
    resolved_base_sha: str | None,
    policy: SelectionPolicy,
    impact_map: dict | None,
    codegraph_tests: Iterable[str],
    codegraph_problem: str | None,
    repo_root: Path = ROOT,
) -> dict[str, object]:
    """Pure hybrid resolution. Returns a ``mac.sanity_selection.v1`` document."""
    changed = sorted({path for path in changed_files if path})
    if not changed:
        return _full("no_changed_file_scope", changed)

    global_full = sorted(path for path in changed if path in policy.global_full_paths)
    if global_full:
        return _full("global_infrastructure_changed", changed, global_files=global_full)

    test_changes = _existing(
        (path for path in changed if _is_test_file(path)), repo_root
    )
    source_changes = [path for path in changed if _is_source_code(path)]
    opaque = [
        path
        for path in changed
        if not _is_test_file(path)
        and not _is_source_code(path)
        and not _is_documentation(path)
    ]
    if opaque:
        # Unattributable non-code (shell/CI/data/config): cannot be tied to the
        # tests that exercise it, so the only safe scope is the full suite.
        return _full("unmappable_non_code_change", changed, opaque_files=sorted(opaque))

    map_fresh = bool(
        impact_map is not None
        and resolved_base_sha is not None
        and impact_map.get("base_sha") == resolved_base_sha
    )
    nodeids = impact_map.get("nodeids", []) if impact_map else []
    file_tests = impact_map.get("file_tests", {}) if impact_map else {}
    file_line_tests = impact_map.get("file_line_tests", {}) if impact_map else {}

    selected: set[str] = set(test_changes)
    unresolved_source: list[str] = []
    for path in source_changes:
        if map_fresh and path in file_tests:
            base_lines = changed_base_lines.get(path)
            if base_lines:
                line_index = file_line_tests.get(path, {})
                for line in base_lines:
                    for idx in line_index.get(str(line), []):
                        selected.add(nodeids[idx])
            else:
                # Additions-only (no base line to intersect): fall back to every
                # test that executed the file at the base revision.
                for idx in file_tests[path]:
                    selected.add(nodeids[idx])
        else:
            unresolved_source.append(path)

    # CodeGraph is unioned in for every source change: it is the safety net for
    # newly-reachable code the base-revision map cannot know about.
    selected.update(codegraph_tests)

    # Fail closed: a source file the dynamic map could not resolve requires a
    # usable CodeGraph result; otherwise we cannot prove the scope.
    if unresolved_source and (codegraph_problem is not None or not list(codegraph_tests)):
        return _full(
            codegraph_problem or "unresolved_source_without_reliable_affected_tests",
            changed,
            unresolved_source=sorted(unresolved_source),
        )

    # Cross-cutting guards run alongside any real code/test change, but never
    # for a pure documentation change (which needs no tests at all).
    if source_changes or test_changes:
        always = set(policy.always_run)
        if impact_map:
            always.update(impact_map.get("always_run", []))
        selected.update(_existing(always, repo_root))

    if not selected:
        return {
            "schema": SCHEMA,
            "mode": "focused",
            "reason": "non_code_change" if not source_changes else "no_tests_exercise_changed_code",
            "changed_files": changed,
            "tests": [],
            "map_fresh": map_fresh,
        }

    return {
        "schema": SCHEMA,
        "mode": "focused",
        "reason": "impact_hybrid_scope",
        "changed_files": changed,
        "tests": sorted(selected),
        "map_fresh": map_fresh,
        "codegraph_problem": codegraph_problem,
    }


# --------------------------------------------------------------------------
# Git + artifact gathering (IO layer)
# --------------------------------------------------------------------------


def _git(argv: list[str], repo_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv], cwd=repo_root, capture_output=True, text=True, check=False
    )


def _resolve_sha(ref: str | None, repo_root: Path) -> str | None:
    if not ref:
        return None
    result = _git(["rev-parse", "--verify", f"{ref}^{{commit}}"], repo_root)
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else None


def git_changed_files(base: str | None, repo_root: Path) -> list[str]:
    rng = f"{base}...HEAD" if base else "HEAD"
    result = _git(["diff", "--name-only", rng], repo_root)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip())
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def changed_base_lines(base: str | None, repo_root: Path) -> dict[str, set[int]]:
    """Base-side line numbers touched by the diff, per file.

    Uses ``-U0`` so only the exact changed lines appear. Pure additions (hunk
    length 0 on the base side) contribute no base line, which the resolver reads
    as "fall back to file-level for this file"."""
    rng = f"{base}...HEAD" if base else "HEAD"
    result = _git(
        ["diff", "-U0", "--no-color", "--no-ext-diff", rng], repo_root
    )
    if result.returncode != 0:
        return {}
    lines: dict[str, set[int]] = {}
    current: str | None = None
    for row in result.stdout.splitlines():
        if row.startswith("+++ "):
            target = row[4:].strip()
            current = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            continue
        if current is None:
            continue
        match = _HUNK_RE.match(row)
        if match:
            start = int(match.group(1))
            length = int(match.group(2)) if match.group(2) is not None else 1
            if length > 0:
                lines.setdefault(current, set()).update(range(start, start + length))
    return lines


def codegraph_affected(source_changes: list[str], repo_root: Path) -> tuple[list[str], str | None]:
    codegraph = shutil.which("codegraph")
    if not codegraph:
        return [], "codegraph_unavailable"
    if not source_changes:
        return [], None
    completed = subprocess.run(
        [codegraph, "affected", "--json", *source_changes],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        return [], "codegraph_affected_failed"
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return [], "codegraph_affected_invalid_json"
    tests = [
        str(path)
        for path in document.get("affectedTests", [])
        if str(path).startswith(("tests/", "plugin/")) and (repo_root / str(path)).is_file()
    ]
    return sorted(set(tests)), None


def load_map(map_path: Path) -> dict | None:
    try:
        document = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def select_from_git(
    *,
    base: str | None,
    repo_root: Path = ROOT,
    policy: SelectionPolicy | None = None,
    changed: list[str] | None = None,
    codegraph: Callable[[list[str], Path], tuple[list[str], str | None]] = codegraph_affected,
) -> dict[str, object]:
    policy = policy or load_policy()
    try:
        changed_files = changed if changed is not None else git_changed_files(base, repo_root)
        base_lines = changed_base_lines(base, repo_root)
    except (OSError, RuntimeError) as exc:
        return _full("selection_error", [], error=str(exc))
    source_changes = [path for path in changed_files if _is_source_code(path)]
    cg_tests, cg_problem = codegraph(source_changes, repo_root)
    return resolve(
        changed_files,
        base_lines,
        resolved_base_sha=_resolve_sha(base, repo_root),
        policy=policy,
        impact_map=load_map(policy.map_path),
        codegraph_tests=cg_tests,
        codegraph_problem=cg_problem,
        repo_root=repo_root,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--tests-only", action="store_true")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--repo-root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    result = select_from_git(
        base=args.base,
        repo_root=args.repo_root.resolve(),
        policy=load_policy(args.policy),
        changed=args.changed_file or None,
    )
    if args.tests_only:
        for path in result["tests"]:
            print(path)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
