#!/usr/bin/env python3
"""Select a fail-closed PR sanity scope from changed files and CodeGraph."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "mac.sanity_selection.v1"
CANARIES = (
    "tests/test_control_plane_public_contract.py",
    "tests/api/test_task_read_endpoints.py",
    "tests/cli/test_cli_coverage_gate.py",
    "tests/ui/test_fleet_ide_api_contracts.py",
    "tests/test_worker_process_e2e.py",
)
BROAD_PATHS = {
    "Makefile",
    "conftest.py",
    "pyproject.toml",
    "uv.lock",
    "test-policy.toml",
    "tests/conftest.py",
    "scripts/run-contract-tests.sh",
    "scripts/run-sanity-tests.sh",
    "scripts/select-sanity-tests.py",
}
BROAD_PREFIXES = (
    ".github/workflows/",
    "deploy/codex-runner/",
    "scripts/",
    "tests/fault_replay/",
)


def _git_changed_files(base: str | None) -> list[str]:
    if base:
        command = ["git", "diff", "--name-only", f"{base}...HEAD"]
    else:
        command = ["git", "diff", "--name-only", "HEAD"]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError((result.stdout + result.stderr).strip())
    return sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})


def _module_test_candidates(path: str) -> list[str]:
    source = Path(path)
    if path.startswith("src/mac/") and source.suffix == ".py":
        stem = source.stem
        direct = ROOT / "tests" / f"test_{stem}.py"
        candidates = [str(direct.relative_to(ROOT))] if direct.exists() else []
        candidates.extend(
            str(item.relative_to(ROOT))
            for item in sorted((ROOT / "tests").glob(f"test_{stem}_*.py"))
        )
        return candidates
    return []


def _codegraph_affected(changed: Iterable[str]) -> tuple[list[str], str | None]:
    codegraph = shutil.which("codegraph")
    if not codegraph:
        return [], "codegraph_unavailable"
    source_changes = [path for path in changed if path.startswith("src/")]
    if not source_changes:
        return [], None
    result = subprocess.run(
        [codegraph, "affected", "--json", *source_changes],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return [], "codegraph_affected_failed"
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [], "codegraph_affected_invalid_json"
    tests = [
        str(path)
        for path in document.get("affectedTests", [])
        if str(path).startswith(("tests/", "plugin/")) and (ROOT / str(path)).is_file()
    ]
    return sorted(set(tests)), None


def select(changed: Iterable[str]) -> dict[str, object]:
    changed_files = sorted(set(changed))
    if not changed_files:
        return {
            "schema": SCHEMA,
            "mode": "full",
            "reason": "no_trustworthy_changed_file_scope",
            "changed_files": [],
            "tests": [],
        }
    broad = [
        path
        for path in changed_files
        if path in BROAD_PATHS or path.startswith(BROAD_PREFIXES)
    ]
    if broad:
        return {
            "schema": SCHEMA,
            "mode": "full",
            "reason": "test_or_shared_runtime_infrastructure_changed",
            "changed_files": changed_files,
            "broad_files": broad,
            "tests": [],
        }

    code_changes = [
        path
        for path in changed_files
        if path.startswith(("src/", "plugin/")) and Path(path).suffix in {".py", ".js", ".ts", ".tsx"}
    ]
    directly_changed_tests = [
        path
        for path in changed_files
        if path.startswith(("tests/", "plugin/"))
        and Path(path).suffix == ".py"
        and Path(path).name.startswith("test_")
        and (ROOT / path).is_file()
    ]
    if not code_changes:
        return {
            "schema": SCHEMA,
            "mode": "focused",
            "reason": "non_code_change",
            "changed_files": changed_files,
            "tests": sorted(set(directly_changed_tests)),
        }

    mapped = set(directly_changed_tests)
    for path in code_changes:
        mapped.update(_module_test_candidates(path))
    affected, codegraph_problem = _codegraph_affected(code_changes)
    mapped.update(affected)
    if not mapped:
        return {
            "schema": SCHEMA,
            "mode": "full",
            "reason": codegraph_problem or "code_change_has_no_reliable_affected_tests",
            "changed_files": changed_files,
            "tests": [],
        }
    mapped.update(path for path in CANARIES if (ROOT / path).is_file())
    return {
        "schema": SCHEMA,
        "mode": "focused",
        "reason": "direct_codegraph_and_canary_scope",
        "changed_files": changed_files,
        "tests": sorted(mapped),
        "codegraph_problem": codegraph_problem,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base")
    parser.add_argument("--changed-file", action="append", default=[])
    parser.add_argument("--tests-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        changed = args.changed_file or _git_changed_files(args.base)
        result = select(changed)
    except (OSError, RuntimeError) as exc:
        result = {
            "schema": SCHEMA,
            "mode": "full",
            "reason": "selection_error",
            "error": str(exc),
            "changed_files": [],
            "tests": [],
        }
    if args.tests_only:
        for path in result["tests"]:
            print(path)
    else:
        print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
