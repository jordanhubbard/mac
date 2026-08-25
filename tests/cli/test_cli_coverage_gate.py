"""CLI subcommand coverage gate.

This test prevents silent regression of the CLI subcommand:test ratio.

Strategy
--------
1. Parse ``src/mac/cli.py`` to extract every ``add_parser("<name>")`` call and
   build a set of (domain, subcommand) pairs representing every reachable CLI
   path at the first two levels (e.g. ``("task", "create")``, ``("init", "")``
   for top-level commands with no subparser).

2. Scan all ``tests/cli/test_*.py`` files for ``_run(...)`` invocations and
   build a coverage map of (domain, subcommand) pairs that are exercised.

3. Assert that every discovered subcommand either (a) appears in at least one
   test, or (b) is listed in the explicit ``KNOWN_UNTESTED`` allowlist below.

Operators can shrink the allowlist as new per-domain test modules are merged.

Stretch goal
------------
The ``make cli-coverage`` Makefile target uses the same logic and prints the
coverage ratio as a percentage.
"""

from __future__ import annotations

import re
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cli_py_path() -> Path:
    """Return the absolute path to src/mac/cli.py."""
    here = Path(__file__).resolve()
    # tests/cli/test_cli_coverage_gate.py -> repo root -> src/mac/cli.py
    repo_root = here.parent.parent.parent
    return repo_root / "src" / "mac" / "cli.py"


def _cli_test_dir() -> Path:
    return Path(__file__).resolve().parent


def discover_cli_subcommands(cli_src: str) -> set[tuple[str, str]]:
    """Parse *cli_src* and return the set of (domain, subcommand) pairs.

    The parser is intentionally simple: it matches every
    ``<parent>.add_parser("<name>")`` occurrence, identifies which calls
    are top-level (parent variable == ``sub``), then enumerates their
    immediate children.

    Top-level commands that have *no* subparsers (e.g. ``init``,
    ``diagnostics``) are represented as ``(command, "")``.

    Three-level commands (e.g. ``openshell policy create``) are collapsed
    to their first two levels (``("openshell", "policy")``) because that
    is the granularity at which ``_run(...)`` invocations are written.
    """
    # Step 1: collect (var, parent_var, subcommand_name) for every
    #         <var> = <parent>.add_parser("<name>") line.
    var_pattern = re.compile(r"(\w+)\s*=\s*(\w+)\.add_parser\(\s*[\"']([^\"']+)[\"']")
    var_matches = var_pattern.findall(cli_src)

    # Step 2: also collect ALL <parent>.add_parser("<name>") occurrences
    #         (including anonymous forms like ``_set(fn, sub.add_parser("init", ...))``)
    #         by matching the broader pattern that captures every call regardless
    #         of whether the result is assigned to a variable.
    anon_pattern = re.compile(r"(\w+)\.add_parser\(\s*[\"']([^\"']+)[\"']")
    anon_parent_children: dict[str, list[str]] = {}
    for parent, name in anon_pattern.findall(cli_src):
        anon_parent_children.setdefault(parent, [])
        anon_parent_children[parent].append(name)

    # Map domain_name -> list of var names it was assigned to.
    domain_name_to_vars: dict[str, list[str]] = {}
    for var, parent, name in var_matches:
        if parent == "sub":
            domain_name_to_vars.setdefault(name, [])
            domain_name_to_vars[name].append(var)

    # Map parent_var -> list of child subcommand names (from var= forms).
    var_parent_children: dict[str, list[str]] = {}
    for var, parent, name in var_matches:
        var_parent_children.setdefault(parent, [])
        var_parent_children[parent].append(name)

    # Step 3: list all top-level commands (direct children of `sub`).
    top_level_names: list[str] = anon_parent_children.get("sub", [])

    # Step 4: build (domain, subcommand) pairs.
    pairs: set[tuple[str, str]] = set()
    for domain_name in top_level_names:
        subs: list[str] = []
        for dvar in domain_name_to_vars.get(domain_name, []):
            # Children from var= assignments
            subs.extend(var_parent_children.get(dvar, []))
            # Children from anonymous calls (unlikely but safe)
            subs.extend(anon_parent_children.get(dvar, []))

        if subs:
            for sub in set(subs):
                pairs.add((domain_name, sub))
        else:
            # Standalone top-level command with no subparser.
            pairs.add((domain_name, ""))

    return pairs


def discover_tested_subcommands(test_dir: Path) -> set[tuple[str, str]]:
    """Scan test_*.py files for ``_run(...)`` calls and return covered pairs.

    Recognises patterns like::

        _run(tmp_path, "task", "create", ...)
        _run(tmp_path, "admin", "init")

    Returns a set of (domain, subcommand) pairs where subcommand is ``""``
    for top-level invocations with no second positional argument.
    """
    run_pattern = re.compile(
        r'_run\s*\([^,)]+,\s*["\']([^"\']+)["\']'  # captures domain
        r'(?:\s*,\s*["\']([^"\']+)["\'])?'  # optionally captures sub
        r'(?:\s*,\s*["\']([^"\']+)["\'])?'  # and a third, for `admin`
    )
    covered: set[tuple[str, str]] = set()
    for test_file in sorted(test_dir.glob("test_*.py")):
        content = test_file.read_text(encoding="utf-8")
        for match in run_pattern.finditer(content):
            first, second, third = match.group(1), match.group(2), match.group(3)
            # The administrative commands moved under `mac admin`, so their
            # calls read _run(tmp, "admin", "optimizer", "status"). Without
            # this the gate sees domain="admin" for all of them and reports
            # every one as untested -- fifty false alarms, which would train
            # whoever hits them to add allowlist entries instead of tests.
            if first == "admin" and second:
                covered.add((second, third or ""))
                covered.add((first, second))
                continue
            covered.add((first, second or ""))
    return covered


# ---------------------------------------------------------------------------
# Allowlist of subcommands that are not yet covered by a dedicated test.
# Operators should remove entries here as new test modules are added.
# Each entry is a (domain, subcommand) pair.  Use "" for top-level commands
# that have no subparser (e.g. ("diagnostics", "")).
# ---------------------------------------------------------------------------
KNOWN_UNTESTED: frozenset[tuple[str, str]] = frozenset(
    [
        # fleet model-selection: hub-only (calls the running ModelSelectionService,
        # not the local ControlPlane), so it can't run in the --db CLI harness;
        # the endpoints it wraps are exercised by the API route-coverage gate.
        ("fleet", "model-selection"),
        # action-events domain
        ("action-events", "export-otlp"),
        ("action-events", "list"),
        ("action-events", "stream"),
        # agent sub-commands
        ("agent", "config"),
        ("agent", "delete"),
        ("agent", "deregister"),
        ("agent", "tell"),
        ("agent", "hardware"),
        ("agent", "heartbeat"),
        ("agent", "migrate"),
        # agentbus sub-commands (open/append/close covered in test_mac_cli.py indirectly;
        # explicit coverage of the remaining commands still needed)
        ("agentbus", "append"),
        ("agentbus", "artifact-publish"),
        ("agentbus", "close"),
        ("agentbus", "list"),
        ("agentbus", "open"),
        ("agentbus", "publish"),
        ("agentbus", "repo-update"),
        # artifact domain
        ("artifact", "delete"),
        ("artifact", "list"),
        ("artifact", "register"),
        ("artifact", "show"),
        # binding domain
        ("binding", "register"),
        # bridge domain
        ("bridge", "import"),
        ("bridge", "list"),
        ("bridge", "repository"),
        # command-audit domain
        ("command-audit", "list"),
        # config domain
        ("config", "migrate-env-namespace"),
        # diagnostics top-level (no subcommand)
        ("diagnostics", ""),
        # dispatch domain
        ("dispatch", "assign"),
        ("dispatch", "tick"),
        # env domain
        ("env", "current"),
        ("env", "deploy"),
        ("env", "history"),
        ("env", "list"),
        ("env", "register"),
        ("env", "show"),
        # eval domain
        ("eval", "run"),
        ("eval", "set"),
        # events domain
        ("events", "list"),
        # fleet domain
        ("fleet", "build-distribution"),
        ("fleet", "doctor"),
        ("fleet", "memory-export"),
        ("fleet", "memory-prune"),
        ("fleet", "move-agent"),
        ("fleet", "refresh-context"),
        ("fleet", "rotate-token"),
        ("fleet", "snapshot"),
        ("fleet", "soul-pull"),
        ("fleet", "soul-audit"),
        ("fleet", "soul-push"),
        ("fleet", "sync-token"),
        ("fleet", "validate"),
        # hermes domain
        ("persona-instance", "context"),
        ("persona-instance", "register"),
        ("persona-instance", "runtime-proof"),
        ("persona-instance", "work-context"),
        # integrations domain
        ("integrations", "findings"),
        ("integrations", "observations"),
        # interaction domain
        ("interaction", "task"),
        # journal domain
        ("journal", "list"),
        ("journal", "restore"),
        ("journal", "snapshot"),
        # memory domain -- recall-dreams and backfill need external deps
        ("memory", "backfill"),
        ("memory", "embed"),
        ("memory", "recall-dreams"),
        # message domain
        ("message", "inbox"),
        ("message", "send"),
        # migrate domain
        ("migrate", "import"),
        # nap consolidate/cycle require an LLM for memory summarisation;
        # the simpler nap lifecycle commands are covered in test_cli_nap.py
        ("nap", "consolidate"),
        ("nap", "cycle"),
        # notifier domain
        ("notifier", "configure"),
        ("notifier", "delete"),
        ("notifier", "deliver"),
        ("notifier", "list"),
        # observability domain
        ("observability", "list"),
        ("observability", "prune"),
        # openshell sub-commands not yet covered
        ("openshell", "policy"),
        ("openshell", "render-policy"),
        # persona domain
        ("persona", "register"),
        # project sub-commands
        ("project", "activate"),
        ("project", "pause"),
        ("project", "show"),
        # publish top-level (no subcommand)
        ("publish", ""),
        # pull-request domain
        ("pull-request", "open"),
        # review domain
        ("review", "decision"),
        ("review", "request"),
        # rollout domain
        ("rollout", "advance"),
        ("rollout", "create"),
        ("rollout", "health"),
        ("rollout", "list"),
        ("rollout", "rescue"),
        ("rollout", "verify-artifact"),
        # runtime sub-commands
        ("runtime", "list"),
        # secret sub-commands
        ("secret", "access"),
        ("secret", "audits"),
        ("secret", "delete"),
        ("secret", "rotate"),
        # task sub-commands -- only genuinely untested ones remain
        ("task", "convert-ticketing"),
        ("task", "detect-beads"),
        ("task", "detect-ticketing"),
        ("task", "force-complete"),
        ("task", "migrate-beads"),
        ("task", "submit-review"),
        # user domain
        ("user", "register"),
        # workflow domain
        ("workflow", "decisions"),
        ("workflow", "start"),
    ]
)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cli_subcommand_coverage_gate():
    """Every CLI subcommand must either have a test or be listed in KNOWN_UNTESTED.

    Fails with a clear list of any subcommands that slip through the gap.
    This gate is intentionally strict: adding a new subcommand to cli.py
    *requires* updating the allowlist (or adding a test), making regressions
    visible at CI time.
    """
    cli_src = _cli_py_path().read_text(encoding="utf-8")
    all_subcommands = discover_cli_subcommands(cli_src)
    tested = discover_tested_subcommands(_cli_test_dir())

    # A subcommand passes if it is explicitly tested OR in the allowlist.
    covered = tested | KNOWN_UNTESTED

    missing = sorted(all_subcommands - covered)
    if missing:
        lines = ["CLI subcommands without a test AND not in KNOWN_UNTESTED:"]
        for domain, sub in missing:
            cmd = f"mac {domain} {sub}".strip()
            lines.append(f"  {cmd}")
        lines.append("")
        lines.append(
            "Either add a test in tests/cli/test_*.py that calls "
            "_run(tmp_path, <domain>, <subcommand>, ...) or add the pair "
            "to KNOWN_UNTESTED in this file."
        )
        raise AssertionError("\n".join(lines))


def test_known_untested_allowlist_is_subset_of_discovered():
    """Every entry in KNOWN_UNTESTED must correspond to an actual CLI subcommand.

    This prevents stale allowlist entries from accumulating silently after a
    subcommand is renamed or removed.
    """
    cli_src = _cli_py_path().read_text(encoding="utf-8")
    all_subcommands = discover_cli_subcommands(cli_src)

    stale = sorted(KNOWN_UNTESTED - all_subcommands)
    if stale:
        lines = ["KNOWN_UNTESTED contains entries that no longer exist in cli.py:"]
        for domain, sub in stale:
            cmd = f"mac {domain} {sub}".strip()
            lines.append(f"  {cmd}")
        lines.append("")
        lines.append("Remove these stale entries from KNOWN_UNTESTED.")
        raise AssertionError("\n".join(lines))


def test_cli_coverage_ratio():
    """Report the current coverage ratio (informational – always passes).

    The ratio = (tested subcommands / total subcommands) * 100.
    Run ``make cli-coverage`` for a quick human-readable report.
    """
    cli_src = _cli_py_path().read_text(encoding="utf-8")
    all_subcommands = discover_cli_subcommands(cli_src)
    tested = discover_tested_subcommands(_cli_test_dir())

    # Exclude self from tested (we don't call _run in this file)
    actually_tested = tested & all_subcommands
    total = len(all_subcommands)
    covered_count = len(actually_tested)
    ratio = (covered_count / total * 100) if total else 0.0

    print(
        f"\nCLI coverage: {covered_count}/{total} subcommands tested "
        f"({ratio:.1f}%); "
        f"{len(KNOWN_UNTESTED)} in allowlist, "
        f"{total - covered_count - len(KNOWN_UNTESTED & all_subcommands)} gap."
    )
    # This test always passes; it is purely informational.
    assert total > 0, "No subcommands found — cli.py may have changed structure"
