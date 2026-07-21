#!/usr/bin/env python3
"""Fail-closed PR sanity scope — thin adapter over the impact-based resolver.

Historically this selector mapped a changed src file to tests by NAME
convention and escalated to a full run on any change under broad prefixes
(`scripts/`, `deploy/codex-runner/`, `tests/fault_replay/`, `pyproject.toml`,
...). That over-escalation is exactly why rollouts ran the whole suite.

Selection now delegates to scripts/resolve-impacted-tests.py, which maps a
source change to the tests whose COVERAGE actually touched it (dynamic map),
unions CodeGraph static reachability, and only falls back to a full run when a
changed file cannot be safely attributed. The only categorical full-run
triggers now live in the resolver's `[selection].global_full_paths` (files that
invalidate the whole map or collection) plus genuinely opaque non-code files.

Output remains the ``mac.sanity_selection.v1`` document consumed by
scripts/run-sanity-tests.sh, so this drop-in preserves the rollout contract.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path
from typing import Callable, Iterable

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "mac.sanity_selection.v1"
_RESOLVER_NAME = "mac_resolve_impacted_tests"


def _resolver():
    """Import the sibling resolver module (hyphenated file name)."""
    if _RESOLVER_NAME in sys.modules:
        return sys.modules[_RESOLVER_NAME]
    path = ROOT / "scripts" / "resolve-impacted-tests.py"
    spec = importlib.util.spec_from_file_location(_RESOLVER_NAME, path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load resolve-impacted-tests.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_RESOLVER_NAME] = module  # register so dataclasses resolve
    spec.loader.exec_module(module)
    return module


def select(
    changed: Iterable[str],
    *,
    base: str | None = None,
    policy=None,
    codegraph: Callable | None = None,
) -> dict[str, object]:
    """Impact-based selection for an explicit change set."""
    resolver = _resolver()
    scope = sorted({path for path in changed if path})
    kwargs: dict[str, object] = {
        "base": base,
        "repo_root": ROOT,
        "changed": scope,
        "policy": policy if policy is not None else resolver.load_policy(),
    }
    if codegraph is not None:
        kwargs["codegraph"] = codegraph
    return resolver.select_from_git(**kwargs)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--tests-only", action="store_true")
    args = parser.parse_args(argv)
    resolver = _resolver()
    result = resolver.select_from_git(
        base=args.base,
        repo_root=ROOT,
        policy=resolver.load_policy(),
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
