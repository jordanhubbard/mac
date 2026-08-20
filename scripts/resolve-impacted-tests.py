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
3. Reviewed path contracts: generated data, shell entrypoints, and CI files
   select the repository tests that own their behavior. The mapping is kept in
   this executable so changing the selector itself also has an explicit test.
4. Full-suite fallback: any changed file that none of those layers can safely
   map (stale/missing map entry plus unusable CodeGraph, an unknown opaque infra
   file, or a globally invalidating file) forces a full run.

`resolve()` is a pure function (no git, no IO) so the safety matrix is fully
unit-tested; `select_from_git()` gathers the git diff, map, and CodeGraph and
delegates to it. Output is the existing ``mac.sanity_selection.v1`` document so
it drops straight into scripts/run-sanity-tests.sh.
"""

from __future__ import annotations

import argparse
import json
import ast
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
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

# Exact contracts for non-source paths that coverage and CodeGraph cannot map.
# This list is intentionally code-reviewed and exact-path only: unknown shell,
# CI, data, and configuration files must continue to force a full run. Map both
# sides of generated artifacts so a generator or generated-output-only change
# cannot silently bypass its drift test.
PATH_TEST_CONTRACTS: dict[str, tuple[str, ...]] = {
    ".github/workflows/ci.yml": ("tests/test_deployment_image_artifact.py",),
    "deploy/deploy-mac-fleet.sh": (
        "tests/test_deploy_fleet_drain.py",
        # Owns the phase-1 quiescence requirement this script hands the node:
        # a from-scratch first-hub bootstrap must not be told to prove a
        # receipt that only an upgrade of a prior generation can produce.
        "tests/test_first_hub_bootstrap_phase1_quiescence.py",
        "tests/test_fleet_node_machine_onboard.py",
        "tests/test_reviewed_openshell_cli.py",
    ),
    "deploy/fleet-node-machine-onboard.py": (
        "tests/test_fleet_node_machine_onboard.py",
    ),
    "deploy/fleet-node-phase1-quiesce.sh": (
        "tests/test_fleet_node_phase1_quiesce.py",
    ),
    "deploy/fleet-node-install.sh": (
        # Guards that this script stays the WRITER of the startup self-test and
        # never becomes another reader of the dispatch-readiness rule; it scans
        # every deploy/scripts shell file for inline copies.
        "tests/test_agent_health_is_one_check.py",
        "tests/test_codegraph_runtime_baseline.py",
        "tests/test_declared_extras_exist.py",
        "tests/test_container_runtime_declaration.py",
        "tests/test_crash_observer.py",
        "tests/test_deploy_agent_configs.py",
        "tests/test_deploy_direct_hub_readiness.py",
        "tests/test_deploy_fleet_drain.py",
        "tests/test_deploy_fleet_parallel_staging.py",
        "tests/test_deploy_github_https_credentials.py",
        "tests/test_first_hub_bootstrap_phase1_quiescence.py",
        "tests/test_fleet_node_capability_truthfulness.py",
        "tests/test_fleet_node_daemon_quiescence.py",
        "tests/test_fleet_node_gateway_readiness.py",
        "tests/test_fleet_node_generated_rollback.py",
        "tests/test_fleet_node_launchd_prestate.py",
        "tests/test_fleet_node_machine_onboard.py",
        "tests/test_fleet_node_phase1_quiesce.py",
        "tests/test_fleet_node_prior_topology.py",
        "tests/test_fleet_node_supervisord_lifecycle.py",
        "tests/test_fleet_skills.py",
        "tests/test_gateway_probe_blast_radius.py",
        "tests/test_gateway_serving_openclaw_agent_probe_soft.py",
        "tests/test_gateway_serving_worker_selftest_soft_agent_probe.py",
        "tests/test_gatewayless_worker_selftest_crash.py",
        "tests/test_generated_artifact_guards_always_run.py",
        "tests/test_github_review_key_install.py",
        "tests/test_hub_does_not_log_on_the_event_loop.py",
        "tests/test_human_interface_switch_gate.py",
        "tests/test_openclaw_gateway_deploy.py",
        "tests/test_report_repository_routing.py",
        "tests/test_retry_exclusion_ratchet.py",
        "tests/test_reviewed_openshell_cli.py",
        "tests/test_selftest_execution_boundary.py",
        "tests/test_selftest_fleet_scoped_token.py",
        "tests/test_selftest_report_executor_attestation_crash.py",
        "tests/test_selftest_transient_timeout_crash.py",
        "tests/test_task_sandbox_reaping_policy.py",
        "tests/test_worker_control_edges.py",
        # The worker's shutdown grace must stay inside this installer's
        # TimeoutStopSec, or the lease is never released before SIGKILL.
        # The test asserts on that relationship, so lowering the unit's
        # timeout here has to run it.
        "tests/test_worker_shutdown_abandon.py",
        # Added with the container-runtime declaration (#291) and the
        # Covers the worker side of the runtime-marker contract this installer
        # a change to it must select them.
        # human-interface switch gate: both assert on this installer, so
        # that decide whether a worker may adopt them.
        # writes: a change to how the markers are produced must run the tests
    ),
    "deploy/fleet-node-rollback-supervisor.py": (
        "tests/test_fleet_node_rollback_supervisor.py",
    ),
    "deploy/fleet-node-substrate-adopt.py": (
        "tests/test_fleet_node_substrate_adopt.py",
    ),
    "docs/env-config-reference.md": ("tests/test_env_config.py",),
    "scripts/generate-env-config-registry.py": ("tests/test_env_config.py",),
    # The documentation site's nav. It is a .yml, so it was "opaque" and forced
    # the whole 11,400-test suite: a stale nav entry pointing at a deleted doc
    # failed CI 48 minutes in, and the one-line fix cost another 48. The tests
    # that own it are the ones that read it.
    "mkdocs.yml": ("tests/test_docs_accessibility.py",),
    "scripts/resolve-impacted-tests.py": ("tests/test_resolve_impacted_tests.py",),
    "scripts/run-contract-tests.sh": ("tests/test_contract_test_runner.py",),
    "scripts/select-sanity-tests.py": ("tests/test_resolve_impacted_tests.py",),
    "scripts/test-checkpoint.py": ("tests/test_test_checkpoint.py",),
    "src/mac/data/env_config_registry.json": ("tests/test_env_config.py",),
    # Source entry points the coverage map cannot attribute because they run
    # only out-of-process — a git-invoked askpass helper, or an installed
    # console-script copy whose path is outside the src/ prefix the map indexes.
    # Resolved by their reviewed contract rather than fail-closed (see resolve()).
    "src/mac/git_askpass.py": ("tests/test_git_askpass.py",),
    # The checkpoint rules decide what a resumed gate may SKIP, so a change to
    # them must run their own contract tests even before the nightly portfolio
    # run has taught the impact map about this module.
    "src/mac/test_checkpoint.py": ("tests/test_test_checkpoint.py",),
    "src/mac/investigation_artifacts.py": ("tests/test_per_run_artifact_gitignore.py",),
}


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


def _resolvable(nodeids: Iterable[str], repo_root: Path) -> tuple[list[str], list[str]]:
    """Split node ids into ones pytest can still collect and ones it cannot.

    The impact map is a committed artifact, so it rots: a renamed or deleted
    test stays in it until the map is rebuilt. Handing pytest a node id that no
    longer resolves is a USAGE error (exit 4), not a test failure, so a stale
    entry takes down the run of whoever next touches that file -- with a
    message about the missing test rather than about the map.

    A stale map should cost precision, never correctness. Unresolvable ids are
    dropped and reported.
    """
    import re

    kept: list[str] = []
    dropped: list[str] = []
    cache: dict[str, set[str]] = {}
    for nodeid in nodeids:
        path, sep, rest = nodeid.partition("::")
        if not sep:
            kept.append(nodeid)
            continue
        target = repo_root / path
        if not target.is_file():
            dropped.append(nodeid)
            continue
        names = cache.get(path)
        if names is None:
            try:
                names = set(re.findall(r"^\s*(?:async\s+)?def (test_\w+)",
                                       target.read_text(encoding="utf-8"), re.M))
            except OSError:
                names = set()
            cache[path] = names
        # Strip a class prefix and any parametrisation before comparing.
        leaf = rest.split("[")[0].split("::")[-1]
        (kept if leaf in names else dropped).append(nodeid)
    return kept, dropped


def _scopes(source: str) -> dict[str, tuple[int, int]]:
    """Qualified scope name -> inclusive line span, for every def/class.

    Parse failures return nothing, which the caller reads as "no scope known"
    and answers with the existing file-level fallback.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    scopes: dict[str, tuple[int, int]] = {}

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = prefix + child.name
                end = getattr(child, "end_lineno", None)
                if end is not None:
                    scopes[name] = (child.lineno, end)
                walk(child, name + ".")
            else:
                walk(child, prefix)

    walk(tree, "")
    return scopes


def _innermost(scopes: dict[str, tuple[int, int]], line: int) -> str | None:
    """The narrowest scope containing ``line`` -- the one whose behaviour an
    edit there actually changes."""
    best: str | None = None
    best_span = None
    for name, (start, end) in scopes.items():
        if start <= line <= end:
            span = end - start
            if best_span is None or span < best_span:
                best, best_span = name, span
    return best


def _executable_lines(source_lines: list[str], lines: set[int]) -> set[int]:
    """``lines`` minus blanks and comments.

    Not a micro-optimisation: a new top-level function's diff hunk begins with
    the two blank lines before its ``def``, which sit at module level. Counting
    those as module-level changes sent nearly every addition straight back to
    the whole file.
    """
    kept = set()
    for line in lines:
        if 1 <= line <= len(source_lines):
            text = source_lines[line - 1].strip()
            if not text or text.startswith("#"):
                continue
        kept.add(line)
    return kept


def _touched_scopes(
    scopes: dict[str, tuple[int, int]], source: str, lines: set[int]
) -> set[str] | None:
    """Qualified names of the scopes these lines fall in.

    ``None`` means at least one line is executable code at MODULE level, which
    runs at import time for every importer -- nothing narrower than the whole
    file is honest about that.
    """
    names: set[str] = set()
    for line in _executable_lines(source.splitlines(), lines):
        name = _innermost(scopes, line)
        if name is None:
            return None
        names.add(name)
    return names


def touched_scope_names(
    path: str,
    selection_base: str | None,
    map_base: str | None,
    new_lines: set[int],
    base_lines: set[int],
    repo_root: Path,
) -> set[str] | None:
    """Qualified scopes this file's diff touched, or None when unresolvable.

    THE PROBLEM THIS SOLVES. The committed map is built at one revision, and
    line-level data is only usable for files that are byte-identical at that
    revision. src/mac/cli.py changes in most weeks, so it is almost never
    identical -- and a file whose line data is unusable resolves to the FULL
    suite. Sixteen of the last sixty commits on main selected all 11,020 tests
    and every one of them was cli.py, at roughly an hour of CI each.

    Line NUMBERS drift. Scope NAMES do not. So instead of intersecting line
    numbers -- which requires the map to be current -- this locates the
    functions and classes the diff touched, by qualified name, and charges the
    diff to the lines those same names occupied at the map's base revision.
    The tests attributed to those lines are the tests that execute that code.

    Three ways it declines to answer, all of them fail-closed:
      * a touched line is executable at module level  -> import-time effect
      * either revision of the file will not parse    -> no scopes to reason on
      * the file is absent at the map base            -> nothing to charge to
    """
    new = _git(["show", "HEAD:%s" % path], repo_root)
    old = _git(["show", "%s:%s" % (selection_base or "HEAD", path)], repo_root)
    mapped = _git(["show", "%s:%s" % (map_base, path)], repo_root) if map_base else None
    if new.returncode != 0 or old.returncode != 0 or mapped is None or mapped.returncode != 0:
        return None

    new_scopes = _scopes(new.stdout)
    old_scopes = _scopes(old.stdout)
    if not new_scopes and not old_scopes:
        return None

    touched: set[str] = set()
    # Additions and modifications, located in the file as it now stands.
    added = _touched_scopes(new_scopes, new.stdout, new_lines)
    if added is None:
        return None
    touched |= added
    # Deletions and modifications, located in the file as it was. A scope that
    # was edited away entirely still has to select the tests that ran it.
    removed = _touched_scopes(old_scopes, old.stdout, base_lines)
    if removed is None:
        return None
    touched |= removed

    return touched


def _full(reason: str, changed: list[str], **extra: object) -> dict[str, object]:
    return {"schema": SCHEMA, "mode": "full", "reason": reason, "changed_files": changed, "tests": [], **extra}


def resolve(
    changed_files: Iterable[str],
    changed_base_lines: dict[str, set[int]],
    *,
    addition_points: dict[str, set[int]] | None = None,
    selection_base: str | None = None,
    fresh_map_files: Iterable[str] | None,
    policy: SelectionPolicy,
    impact_map: dict | None,
    codegraph_tests: Iterable[str],
    codegraph_problem: str | None,
    repo_root: Path = ROOT,
) -> dict[str, object]:
    """Pure hybrid resolution. Returns a ``mac.sanity_selection.v1`` document.

    ``fresh_map_files`` is the set of mapped source files whose line/file index
    is trustworthy for THIS selection base — computed by the IO layer
    (``_fresh_map_files``): every mapped file on an exact base match, the subset
    unchanged since the map's ``base_sha`` on an ancestor base, or empty when the
    map is stale/divergent. A source file outside this set falls through to
    CodeGraph/full, so the map is used per-file, never all-or-nothing."""
    changed = sorted({path for path in changed_files if path})
    if not changed:
        return _full("no_changed_file_scope", changed)

    global_full = sorted(path for path in changed if path in policy.global_full_paths)
    if global_full:
        return _full("global_infrastructure_changed", changed, global_files=global_full)

    contracted_changes = [path for path in changed if path in PATH_TEST_CONTRACTS]
    contracted_tests: set[str] = set()
    missing_contract_tests: dict[str, list[str]] = {}
    for path in contracted_changes:
        expected = PATH_TEST_CONTRACTS[path]
        existing = set(_existing(expected, repo_root))
        contracted_tests.update(existing)
        missing = sorted(set(expected) - existing)
        if missing:
            missing_contract_tests[path] = missing
    if missing_contract_tests:
        return _full(
            "path_test_contract_missing",
            changed,
            missing_contract_tests=missing_contract_tests,
        )

    test_changes = _existing(
        (path for path in changed if _is_test_file(path)), repo_root
    )
    source_changes = [path for path in changed if _is_source_code(path)]
    opaque = [
        path
        for path in changed
        if path not in PATH_TEST_CONTRACTS
        and not _is_test_file(path)
        and not _is_source_code(path)
        and not _is_documentation(path)
    ]
    if opaque:
        # Unattributable non-code (shell/CI/data/config): cannot be tied to the
        # tests that exercise it, so the only safe scope is the full suite.
        return _full("unmappable_non_code_change", changed, opaque_files=sorted(opaque))

    fresh_files = frozenset(fresh_map_files or ())
    nodeids = impact_map.get("nodeids", []) if impact_map else []
    file_tests = impact_map.get("file_tests", {}) if impact_map else {}
    file_line_tests = impact_map.get("file_line_tests", {}) if impact_map else {}
    map_fresh = bool(fresh_files)

    selected: set[str] = set(test_changes) | contracted_tests
    unresolved_source: list[str] = []
    map_base = impact_map.get("base_sha") if impact_map else None
    file_scope_tests = impact_map.get("file_scope_tests", {}) if impact_map else {}

    def _charge_file(path: str) -> None:
        for idx in file_tests.get(path, []):
            selected.add(nodeids[idx])

    def _charge_lines(lines: Iterable[int], path: str) -> bool:
        """Charge by line, reporting whether every line was actually known.

        Lines executed by more than the fanout cap are PRUNED from the line
        index. The builder documents that a pruned line falls back to the file
        index "so a change to a pruned line still selects every test that
        touched the file -- a safe superset, never fewer", and the resolver
        never did that: an unknown line contributed nothing at all. So the
        widely-executed lines -- the ones most likely to break something --
        were the ones selecting the fewest tests.
        """
        line_index = file_line_tests.get(path, {})
        complete = True
        for line in lines:
            hits = line_index.get(str(line))
            if hits is None:
                complete = False
                continue
            for idx in hits:
                selected.add(nodeids[idx])
        return complete

    def _charge_scopes(names: set[str], path: str) -> bool:
        scopes = file_scope_tests.get(path)
        if not scopes:
            return False
        for name in names:
            # A name absent from the map is code the map never saw: it can have
            # no tests attributed, and whatever calls it is part of this same
            # diff and charged where it landed.
            for idx in scopes.get(name, []):
                selected.add(nodeids[idx])
        return True

    for path in source_changes:
        if path not in file_tests:
            if path in PATH_TEST_CONTRACTS:
                # A source entry point the map cannot attribute (runs only
                # out-of-process): its reviewed contract tests, unioned above,
                # are authoritative, so it must not fail closed.
                continue
            unresolved_source.append(path)
            continue

        names = touched_scope_names(
            path,
            selection_base,
            map_base,
            (addition_points or {}).get(path, set()),
            changed_base_lines.get(path) or set(),
            repo_root,
        )
        if names is not None and _charge_scopes(names, path):
            # Scope-level resolution: survives line drift, and is computed
            # before the fanout prune, so it answers for the hot lines too.
            continue

        if path in fresh_files:
            base_lines = changed_base_lines.get(path)
            if base_lines and _charge_lines(base_lines, path):
                continue
            # Either a pure addition with no base line, or a line the index
            # pruned. Neither can be answered more narrowly than the file.
            _charge_file(path)
            continue

        # Drifted, and no scope index to fall back on.
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
    always_selected: list[str] = []
    if source_changes or test_changes or contracted_changes:
        always = set(policy.always_run)
        if impact_map:
            always.update(impact_map.get("always_run", []))
        always_selected = _existing(always, repo_root)
        selected.update(always_selected)

    if not selected:
        return {
            "schema": SCHEMA,
            "mode": "focused",
            "reason": "non_code_change" if not source_changes else "no_tests_exercise_changed_code",
            "changed_files": changed,
            "tests": [],
            "map_fresh": map_fresh,
        }

    resolvable, unresolvable = _resolvable(sorted(selected), repo_root)
    if not resolvable:
        # Everything the map pointed at is gone: fall back rather than run
        # nothing and call it a pass.
        return _full("impact_map_entries_all_stale", changed,
                     stale_tests=sorted(unresolvable))
    # Where the selection came from, not just how big it is.
    #
    # "focused, 11 tests" reads like the map narrowed the work. It did -- but
    # those 11 paths held 713 tests, and 578 of them came from ONE always_run
    # entry (tests/test_control_plane_public_contract.py). That decomposition is
    # what identified the real cost of the in-sandbox gate, and reconstructing
    # it took hand-instrumentation because the resolver reported only a total.
    always_set = set(always_selected)
    return {
        "schema": SCHEMA,
        "mode": "focused",
        "reason": "impact_hybrid_scope",
        "changed_files": changed,
        "tests": resolvable,
        "map_fresh": map_fresh,
        "codegraph_problem": codegraph_problem,
        "stale_tests": sorted(unresolvable),
        "provenance": {
            "always_run": sorted(t for t in resolvable if t in always_set),
            "impact": sorted(t for t in resolvable if t not in always_set),
        },
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


def _nothing_is_committed_on_top(base: str | None, repo_root: Path) -> bool:
    """True when HEAD is the base itself, so a commit-range diff is empty.

    This is the ordinary state of a task sandbox. The agent is given a
    worktree whose `.git` was rebuilt by `git init` plus a single baseline
    commit, and it does its work in the working tree without committing --
    the host finalizer is what commits and publishes. So HEAD IS the base,
    `base...HEAD` is empty, and the selector concluded that the task changed
    nothing and therefore everything had to run.
    """

    if not base:
        return False
    head = _git(["rev-parse", "--verify", "HEAD^{commit}"], repo_root)
    resolved = _resolve_sha(base, repo_root)
    return (
        head.returncode == 0
        and resolved is not None
        and head.stdout.strip() == resolved
    )


def git_changed_files(base: str | None, repo_root: Path) -> list[str]:
    rng = f"{base}...HEAD" if base else "HEAD"
    result = _git(["diff", "--name-only", rng], repo_root)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip())
    changed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    if not changed and _nothing_is_committed_on_top(base, repo_root):
        # Compare the base to what is actually on disk. Two-dot against the
        # working tree, plus untracked files, because a task that ADDS a test
        # leaves it untracked and `git diff` alone would not see it.
        worktree = _git(["diff", "--name-only", base], repo_root)
        if worktree.returncode == 0:
            changed |= {
                line.strip() for line in worktree.stdout.splitlines() if line.strip()
            }
        untracked = _git(
            ["ls-files", "--others", "--exclude-standard"], repo_root
        )
        if untracked.returncode == 0:
            changed |= {
                line.strip() for line in untracked.stdout.splitlines() if line.strip()
            }
    return sorted(changed)


def changed_base_lines(
    base: str | None, repo_root: Path
) -> tuple[dict[str, set[int]], dict[str, set[int]]]:
    """Base-side line numbers touched by the diff, and where additions land.

    Uses ``-U0`` so only the exact changed lines appear. Pure additions (hunk
    length 0 on the base side) contribute no base line; the second return value
    records their insertion POINTS so the resolver can charge them to the scope
    they land in rather than to the whole file."""
    rng = f"{base}...HEAD" if base else "HEAD"
    if base and _nothing_is_committed_on_top(base, repo_root):
        # Same reason as git_changed_files: the work is in the working tree,
        # so a commit-range diff reports no lines and every changed file
        # falls back to whole-file charging.
        rng = base
    result = _git(
        ["diff", "-U0", "--no-color", "--no-ext-diff", rng], repo_root
    )
    if result.returncode != 0:
        return {}, {}
    lines: dict[str, set[int]] = {}
    additions: dict[str, set[int]] = {}
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
            new_start = int(match.group(3))
            new_length = int(match.group(4)) if match.group(4) is not None else 1
            if length > 0:
                lines.setdefault(current, set()).update(range(start, start + length))
            else:
                # A pure addition: hunk length 0 on the base side, so there is
                # no base line to intersect. Record WHERE it lands rather than
                # dropping it -- the insertion point is what lets the enclosing
                # scope be identified instead of charging the whole file.
                additions.setdefault(current, set()).update(
                    range(new_start, new_start + max(new_length, 1))
                )
    return lines, additions


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


def _is_ancestor(ancestor: str, descendant: str, repo_root: Path) -> bool:
    """True iff ``ancestor`` is a first-parent/merge ancestor of ``descendant``."""
    return (
        _git(["merge-base", "--is-ancestor", ancestor, descendant], repo_root).returncode
        == 0
    )


def _changed_between(older: str, newer: str, repo_root: Path) -> set[str] | None:
    """Files whose bytes differ between the two commit TREES (endpoint compare,
    not path history), or None on git error so the caller can fail closed."""
    result = _git(["diff", "--name-only", older, newer], repo_root)
    if result.returncode != 0:
        return None
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _fresh_map_files(
    impact_map: dict | None, resolved_base_sha: str | None, repo_root: Path
) -> frozenset[str]:
    """Mapped source files whose line/file index is valid for this base.

    Exact base match -> every mapped file. Otherwise the map is still usable for
    any mapped file whose content is byte-identical at the map's ``base_sha`` and
    the selection base, but ONLY when ``base_sha`` is an ancestor of that base
    (never trust a map from divergent or rewound history). Any git failure or a
    non-ancestor base yields the empty set, i.e. the strict pre-existing
    fail-closed behavior."""
    if not impact_map or not resolved_base_sha:
        return frozenset()
    map_base = impact_map.get("base_sha")
    mapped = set(impact_map.get("file_tests", {}))
    if not map_base or not mapped:
        return frozenset()
    if map_base == resolved_base_sha:
        return frozenset(mapped)
    if not _is_ancestor(map_base, resolved_base_sha, repo_root):
        return frozenset()
    changed = _changed_between(map_base, resolved_base_sha, repo_root)
    if changed is None:
        return frozenset()
    # A mapped file changed since base_sha => its base-side line numbers no
    # longer align with the map; drop it (it falls through to CodeGraph/full).
    return frozenset(mapped - changed)


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
        base_lines, addition_points = changed_base_lines(base, repo_root)
    except (OSError, RuntimeError) as exc:
        return _full("selection_error", [], error=str(exc))
    source_changes = [path for path in changed_files if _is_source_code(path)]
    cg_tests, cg_problem = codegraph(source_changes, repo_root)
    impact_map = load_map(policy.map_path)
    return resolve(
        changed_files,
        base_lines,
        addition_points=addition_points,
        selection_base=base,
        fresh_map_files=_fresh_map_files(
            impact_map, _resolve_sha(base, repo_root), repo_root
        ),
        policy=policy,
        impact_map=impact_map,
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
