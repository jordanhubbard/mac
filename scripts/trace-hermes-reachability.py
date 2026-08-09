#!/usr/bin/env python3
"""Static import-closure reachability trace for the Hermes runtime (ADR 0001, hu-02).

Computes which first-party Hermes modules are reachable from the entrypoints the
deployed mac admin fleet actually runs (the gateway + the oneshot agent), so the
vendored snapshot manifest in ``deploy/hermes/SNAPSHOT.md`` is a *measurement*
rather than a guess.

Usage:
    scripts/trace-hermes-reachability.py [HERMES_CHECKOUT]   # default: ~/Src/hermes-agent

Caveats (why this is a floor, not the whole truth):
  - Static AST analysis misses lazy/dynamic imports (e.g. tools/lazy_deps.py,
    importlib by-name plugin loading).
  - It misses runtime-loaded *data* trees like ``skills/`` (markdown skills the
    agent scans at runtime, not Python imports) — vendor those explicitly.
  - Confirm with a runtime import trace (python -X importtime on a real gateway
    boot + a oneshot) before deleting anything from the include set.
"""

from __future__ import annotations

import ast
import os
import sys
from collections import Counter, deque

# Entrypoints the deployed gateway + oneshot agent use (see deploy-mac-fleet.sh
# `hermes gateway run` and mac-hermes-task-executor `hermes -z`).
ENTRYPOINTS = [
    "hermes_bootstrap.py",
    "cli.py",
    "gateway/run.py",
    "gateway/session.py",
    "agent/conversation_loop.py",
    "agent/agent_init.py",
    "mcp_serve.py",
    "hermes_cli/runtime_provider.py",
]


def main() -> int:
    root = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/Src/hermes-agent"))
    if not os.path.isdir(root):
        print(f"hermes checkout not found: {root}", file=sys.stderr)
        return 2

    tops = set()
    for n in os.listdir(root):
        p = os.path.join(root, n)
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "__init__.py")):
            tops.add(n)
        elif n.endswith(".py"):
            tops.add(n[:-3])

    def mod_to_path(mod: str):
        parts = mod.split(".")
        for cand in (os.path.join(root, *parts) + ".py", os.path.join(root, *parts, "__init__.py")):
            if os.path.exists(cand):
                return cand
        return None

    def first_party(mod: str) -> bool:
        return mod.split(".")[0] in tops

    entries = [os.path.join(root, e) for e in ENTRYPOINTS if os.path.exists(os.path.join(root, e))]
    seen: set[str] = set()
    reach: set[str] = set(entries)
    q = deque(entries)
    while q:
        f = q.popleft()
        if f in seen:
            continue
        seen.add(f)
        try:
            tree = ast.parse(open(f, encoding="utf-8").read())
        except (OSError, SyntaxError):
            continue
        mods: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods.update(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                mods.add(node.module)
        for m in mods:
            if first_party(m):
                p = mod_to_path(m)
                if p and p not in reach:
                    reach.add(p)
                    q.append(p)

    by_sub = Counter(os.path.relpath(p, root).split(os.sep)[0] for p in reach)
    print(f"hermes checkout: {root}")
    print(f"entrypoints traced: {len(entries)}")
    print("\nREACHABLE first-party files by subtree (vendor these):")
    for k, v in sorted(by_sub.items(), key=lambda x: -x[1]):
        print(f"  {k:22s} {v}")
    print(f"\nTOTAL reachable first-party files: {len(reach)}")

    all_dirs = {n for n in os.listdir(root) if os.path.isdir(os.path.join(root, n)) and not n.startswith(".")}
    reached_dirs = {k for k in by_sub if os.path.isdir(os.path.join(root, k))}
    print("\nTop-level dirs NEVER statically reached (prune candidates; verify skills/ at runtime):")
    print("  " + ", ".join(sorted(all_dirs - reached_dirs)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
