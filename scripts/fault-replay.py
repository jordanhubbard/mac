#!/usr/bin/env python3
"""Prove current probes pass and fail on the source immediately before fixes."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "mac.historical_fault_replay.v1"


def _probe_interpreter() -> str:
    """An interpreter that can actually import the control plane.

    ``sys.executable`` is whatever ran THIS script, and CI invokes it bare --
    ``scripts/fault-replay.py`` -- so the shebang picks the system python3,
    which has no psycopg. That was harmless while ControlPlane.in_memory() used
    SQLite; the Postgres migration made every probe need the driver, and the
    replay started failing with a nested ImportError inside a captured
    subprocess. It runs only in the scheduled nightly, so it was invisible on
    every pull request and surfaced as a red main days later.

    Prefer the project venv, then a uv-managed environment, then whatever is
    running us. Resolved rather than assumed, so the script is correct however
    it is invoked -- through `uv run`, through the shebang, or directly.
    """
    candidates = [
        str(ROOT / ".venv" / "bin" / "python"),
        str(ROOT / ".venv" / "Scripts" / "python.exe"),
        sys.executable,
    ]
    if shutil.which("uv"):
        resolved = subprocess.run(
            ["uv", "run", "python", "-c", "import sys; print(sys.executable)"],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if resolved.returncode == 0 and resolved.stdout.strip():
            candidates.insert(0, resolved.stdout.strip())

    # EXISTING is not the same as USABLE. A first attempt preferred any
    # ROOT/.venv it found, and picked one that existed without the driver --
    # the same "looks configured, is not" failure this replay exists to catch.
    # So each candidate is asked whether it can import the driver, rather than
    # assumed to.
    for candidate in candidates:
        if not candidate:
            continue
        if candidate != sys.executable and not os.access(candidate, os.X_OK):
            continue
        check = subprocess.run(
            [candidate, "-c", "import psycopg"],
            capture_output=True,
            check=False,
        )
        if check.returncode == 0:
            return candidate

    raise SystemExit(
        "no interpreter available that can import psycopg, which every probe "
        "needs since ControlPlane moved off SQLite. Run this through "
        "`uv run scripts/fault-replay.py`, or install the postgres extra."
    )


def _run_probe(probe: Path, source_root: Path) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(source_root / "src")
    env["MAC_NO_TICKET_MIRROR"] = "1"
    return subprocess.run(
        [_probe_interpreter(), str(probe)],
        cwd=source_root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def replay_fault(fault: dict[str, Any]) -> dict[str, Any]:
    fault_id = str(fault["id"])
    probe = ROOT / str(fault["probe"])
    fixed_by = str(fault["fixed_by"])
    fixed_expected = int(fault.get("expected_fixed_exit", 0))
    prefix_expected = int(fault.get("expected_prefix_exit", 1))
    current = _run_probe(probe, ROOT)
    current_output = (current.stdout + current.stderr).strip()[-2000:]
    result: dict[str, Any] = {
        "id": fault_id,
        "fixed_by": fixed_by,
        "probe": str(probe.relative_to(ROOT)),
        "current_exit": current.returncode,
        "current_output": current_output,
        "prefix_revision": fixed_by + "^",
    }
    if current.returncode != fixed_expected:
        result["status"] = "fail"
        result["reason"] = "probe_does_not_pass_on_current_source"
        return result
    fixed_marker = str(fault.get("expected_fixed_output_contains") or "")
    if fixed_marker and fixed_marker not in current_output:
        result["status"] = "fail"
        result["reason"] = "fixed_probe_output_marker_missing"
        return result

    worktree = Path(tempfile.mkdtemp(prefix=f"mac-fault-{fault_id}-"))
    shutil.rmtree(worktree)
    try:
        added = subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), fixed_by + "^"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if added.returncode != 0:
            result["status"] = "fail"
            result["reason"] = "prefix_worktree_unavailable"
            result["prefix_output"] = (added.stdout + added.stderr).strip()[-2000:]
            return result
        prefix = _run_probe(probe, worktree)
        prefix_output = (prefix.stdout + prefix.stderr).strip()[-2000:]
        result["prefix_exit"] = prefix.returncode
        result["prefix_output"] = prefix_output
        if prefix.returncode != prefix_expected:
            result["status"] = "fail"
            result["reason"] = "probe_does_not_distinguish_prefix_source"
            return result
        prefix_marker = str(fault.get("expected_prefix_output_contains") or "")
        if prefix_marker and prefix_marker not in prefix_output:
            result["status"] = "fail"
            result["reason"] = "prefix_fault_output_marker_missing"
            return result
        result["status"] = "pass"
        result["reason"] = "current_passes_and_prefix_reproduces_fault"
        return result
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(worktree)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest", type=Path, default=ROOT / "tests/fault_replay/faults.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    document = json.loads(args.manifest.read_text(encoding="utf-8"))
    if document.get("schema") != "mac.historical_fault_corpus.v1":
        print("fault-replay: unsupported manifest schema", file=sys.stderr)
        return 2
    results = [replay_fault(dict(fault)) for fault in document.get("faults", [])]
    report = {
        "schema": SCHEMA,
        "status": "pass" if results and all(item["status"] == "pass" for item in results) else "fail",
        "faults": results,
    }
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
