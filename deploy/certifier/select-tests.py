#!/usr/bin/env python3
"""Create the frozen, fail-closed certifier execution plan.

The selector is shipped in the immutable certifier image and reads only Git
metadata plus the image-owned test inventory.  Candidate code is never
imported while the plan is being made.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping


SCHEMA = "mac.certifier_phase_manifest.v1"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
MAX_CHANGED_FILES = 4096
MAX_PATH_BYTES = 1024
FULL_TARGETS = ("tests", "plugin/test_tools.py")
IMPACT_MAP_RELATIVE = "src/mac/data/test_impact_map.json"
IMPACT_MAP_SCHEMA = "mac.test_impact_map.v1"
INVARIANT_TESTS = (
    "tests/test_publication_lane.py",
    "tests/test_repository_contract_certification.py",
    "tests/test_openshell_certifier.py",
)
DOC_SUFFIXES = frozenset(
    {
        ".adoc",
        ".gif",
        ".jpeg",
        ".jpg",
        ".md",
        ".pdf",
        ".png",
        ".rst",
        ".svg",
        ".txt",
        ".webp",
    }
)


class SelectionError(RuntimeError):
    """The candidate cannot be scoped without guessing."""


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_path(value: str) -> str:
    if not value or len(value.encode("utf-8")) > MAX_PATH_BYTES:
        raise SelectionError("changed path is empty or too long")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise SelectionError("changed path contains control characters")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or str(path) != value:
        raise SelectionError("changed path is not a canonical repository path")
    return value


def _regular_file(root: Path, relative: str) -> bool:
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        return path.is_file() and not path.is_symlink()
    except OSError:
        return False


def _is_documentation(path: str) -> bool:
    value = PurePosixPath(path)
    return bool(
        path.startswith("docs/")
        or (len(value.parts) == 1 and value.suffix.lower() in DOC_SUFFIXES)
        or (path.startswith(".github/") and value.suffix.lower() in {".md", ".rst", ".txt"})
    )


def _module_tests(path: str, trusted_root: Path) -> tuple[str, ...]:
    source = PurePosixPath(path)
    if not path.startswith("src/mac/") or source.suffix != ".py":
        return ()
    stem = source.stem
    candidates: set[str] = set()
    for pattern in (f"test_{stem}.py", f"test_{stem}_*.py"):
        for item in (trusted_root / "tests").glob(pattern):
            relative = item.relative_to(trusted_root).as_posix()
            if _regular_file(trusted_root, relative):
                candidates.add(relative)
    if stem == "api":
        for item in (trusted_root / "tests" / "api").glob("test_*.py"):
            relative = item.relative_to(trusted_root).as_posix()
            if _regular_file(trusted_root, relative):
                candidates.add(relative)
    return tuple(sorted(candidates))


def _sha256_of(root: Path, relative: str) -> str | None:
    path = root.joinpath(*PurePosixPath(relative).parts)
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def _load_impact_map(path: Path) -> dict | None:
    """Read the image-owned test-impact map. Any defect returns None so the
    selector falls back to its existing (fail-closed) name-convention path."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != IMPACT_MAP_SCHEMA:
        return None
    return payload


def _impact_map_is_fresh(impact_map: Mapping | None, assembly_base_sha: str) -> bool:
    return bool(impact_map and impact_map.get("base_sha") == assembly_base_sha)


def _impact_map_tests(
    source_path: str,
    impact_map: Mapping | None,
    trusted_root: Path,
    assembly_base_sha: str,
) -> tuple[str, ...]:
    """Frozen test files the map attributes to a source file, or () when the map
    cannot be trusted for it (missing, stale base, unmapped, or a content hash
    that disagrees with the trusted image copy)."""
    if not _impact_map_is_fresh(impact_map, assembly_base_sha):
        return ()
    assert impact_map is not None
    indices = (impact_map.get("file_tests") or {}).get(source_path)
    if not indices:
        return ()
    expected = (impact_map.get("file_hashes") or {}).get(source_path)
    if not expected or _sha256_of(trusted_root, source_path) != expected:
        return ()
    nodeids = impact_map.get("nodeids") or []
    tests: set[str] = set()
    for index in indices:
        if not isinstance(index, int) or not 0 <= index < len(nodeids):
            return ()
        test_file = str(nodeids[index]).split("::", 1)[0]
        if _regular_file(trusted_root, test_file):
            tests.add(test_file)
    return tuple(sorted(tests))


def _phase(mode: str, reason: str, tests: Iterable[str] = ()) -> dict[str, object]:
    return {"mode": mode, "reason": reason, "tests": sorted(set(tests))}


def plan_selection(
    changed_files: Iterable[str],
    *,
    trusted_root: Path,
    assembly_base_sha: str,
    candidate_sha: str,
    trusted_source_revision: str,
    impact_map: Mapping | None = None,
) -> dict[str, object]:
    """Return one deterministic plan with at most one full-suite phase.

    The optional ``impact_map`` is the image-owned per-test coverage map. It can
    only REDUCE a full run to a focused one by attributing an otherwise-unmapped
    source file to the frozen tests that exercised it, and only when the map is
    fresh (base SHA matches) and the trusted image copy of the file still hashes
    to the value recorded in the map. Any doubt leaves the file unmapped, so the
    plan degrades to exactly the pre-map fail-closed behavior."""

    if not SHA_RE.fullmatch(assembly_base_sha):
        raise SelectionError("assembly base must be an exact lowercase Git SHA")
    if not SHA_RE.fullmatch(candidate_sha):
        raise SelectionError("candidate must be an exact lowercase Git SHA")
    if not SHA_RE.fullmatch(trusted_source_revision):
        raise SelectionError("trusted source revision is invalid")

    changed = sorted({_safe_path(str(item)) for item in changed_files})
    if not changed:
        raise SelectionError("candidate has no changes relative to its assembly base")
    if len(changed) > MAX_CHANGED_FILES:
        raise SelectionError("candidate changed-file scope exceeds the certifier limit")

    missing_invariants = [item for item in INVARIANT_TESTS if not _regular_file(trusted_root, item)]
    if missing_invariants:
        raise SelectionError("trusted invariant test inventory is incomplete")

    source_changes = [item for item in changed if item.startswith("src/")]
    candidate_test_changes = [
        item
        for item in changed
        if item.startswith("tests/")
        or (
            item.startswith("plugin/")
            and PurePosixPath(item).name.startswith(("test_", "conftest"))
        )
    ]
    non_source_non_docs = [
        item
        for item in changed
        if not item.startswith("src/")
        and item not in candidate_test_changes
        and not _is_documentation(item)
    ]
    changed_frozen_tests = [
        item
        for item in changed
        if item.startswith(("tests/", "plugin/"))
        and PurePosixPath(item).name.startswith("test_")
        and PurePosixPath(item).suffix == ".py"
        and _regular_file(trusted_root, item)
    ]

    mapped: set[str] = set(changed_frozen_tests)
    unmapped_source: list[str] = []
    for source_path in source_changes:
        candidates = _module_tests(source_path, trusted_root)
        if candidates:
            mapped.update(candidates)
            continue
        refined = _impact_map_tests(source_path, impact_map, trusted_root, assembly_base_sha)
        if refined:
            mapped.update(refined)
        else:
            unmapped_source.append(source_path)
    if source_changes and _impact_map_is_fresh(impact_map, assembly_base_sha):
        assert impact_map is not None
        for always in impact_map.get("always_run", []):
            if isinstance(always, str) and _regular_file(trusted_root, always):
                mapped.add(always)

    focused = set(INVARIANT_TESTS)
    focused.update(mapped)
    if unmapped_source and non_source_non_docs:
        authoritative = _phase(
            "rejected",
            "unmapped_source_and_candidate_root_scope_require_two_full_phases",
        )
        supplemental = _phase("skipped", "selection_rejected")
        selection_mode = "mixed_unmapped_rejected"
    elif unmapped_source:
        authoritative = _phase(
            "full",
            "source_change_has_no_frozen_test_mapping",
            FULL_TARGETS,
        )
        supplemental = _phase("skipped", "authoritative_full_is_sufficient")
        selection_mode = "authoritative_full"
    elif non_source_non_docs:
        authoritative = _phase(
            "focused",
            "root_owned_invariants_and_mapped_source_tests",
            focused,
        )
        supplemental = _phase(
            "full",
            "candidate_root_visible_change_requires_supplemental_full",
            FULL_TARGETS,
        )
        selection_mode = "supplemental_full"
    elif source_changes:
        authoritative = _phase(
            "focused",
            "mapped_source_and_root_owned_invariants",
            focused,
        )
        supplemental = _phase("skipped", "no_candidate_root_visible_change")
        selection_mode = "source_focused"
    elif candidate_test_changes:
        authoritative = _phase(
            "focused",
            "candidate_tests_are_non_authoritative_frozen_invariants",
            focused,
        )
        supplemental = _phase("skipped", "candidate_tests_are_worker_evidence")
        selection_mode = "candidate_test_focused"
    else:
        authoritative = _phase(
            "focused",
            "documentation_only_invariants",
            focused,
        )
        supplemental = _phase("skipped", "documentation_only")
        selection_mode = "documentation_fast_lane"

    full_suite_count = sum(phase["mode"] == "full" for phase in (authoritative, supplemental))
    if full_suite_count > 1:
        raise SelectionError("selection attempted more than one full-suite phase")
    payload: dict[str, object] = {
        "schema": SCHEMA,
        "trusted_source_revision": trusted_source_revision,
        "assembly_base_sha": assembly_base_sha,
        "candidate_sha": candidate_sha,
        "changed_files": changed,
        "changed_file_count": len(changed),
        "changed_files_digest": _sha256_json(changed),
        "selection_mode": selection_mode,
        "authoritative": authoritative,
        "supplemental": supplemental,
        "full_suite_count": full_suite_count,
    }
    payload["manifest_digest"] = _sha256_json(payload)
    return payload


def _git(
    candidate_root: Path,
    argv: list[str],
    *,
    allowed_returncodes: tuple[int, ...] = (0,),
) -> bytes:
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-C", str(candidate_root), *argv],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={
            "HOME": "/tmp",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
        },
    )
    if result.returncode not in allowed_returncodes:
        detail = (result.stderr or result.stdout).decode("utf-8", "replace")[:500]
        raise SelectionError("Git scope validation failed: " + detail.strip())
    return result.stdout


def inspect_candidate(candidate_root: Path, assembly_base_sha: str) -> tuple[str, list[str]]:
    candidate_root = candidate_root.resolve(strict=True)
    if not SHA_RE.fullmatch(assembly_base_sha):
        raise SelectionError("assembly base must be an exact lowercase Git SHA")
    candidate_sha = (
        _git(candidate_root, ["rev-parse", "--verify", "HEAD^{commit}"])
        .decode("ascii", "strict")
        .strip()
    )
    if not SHA_RE.fullmatch(candidate_sha):
        raise SelectionError("candidate HEAD is not an exact lowercase Git SHA")
    _git(candidate_root, ["cat-file", "-e", assembly_base_sha + "^{commit}"])
    _git(candidate_root, ["merge-base", "--is-ancestor", assembly_base_sha, candidate_sha])
    raw = _git(
        candidate_root,
        [
            "diff",
            "--name-only",
            "--no-ext-diff",
            "--no-textconv",
            "--diff-filter=ACDMRTUXB",
            "-z",
            assembly_base_sha + "..." + candidate_sha,
        ],
    )
    try:
        changed = [item.decode("utf-8", "strict") for item in raw.split(b"\0") if item]
    except UnicodeDecodeError as exc:
        raise SelectionError("changed paths are not valid UTF-8") from exc
    return candidate_sha, changed


def _load_trusted_revision(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelectionError("trusted harness manifest is unreadable") from exc
    value = payload.get("source_revision") if isinstance(payload, Mapping) else None
    if not isinstance(value, str) or not SHA_RE.fullmatch(value):
        raise SelectionError("trusted harness source revision is invalid")
    return value


def _write(path: Path, content: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-root", type=Path, required=True)
    parser.add_argument("--trusted-root", type=Path, required=True)
    parser.add_argument("--trusted-manifest", type=Path, required=True)
    parser.add_argument("--base-sha", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--modes-output", type=Path, required=True)
    parser.add_argument("--authoritative-tests-output", type=Path, required=True)
    parser.add_argument(
        "--impact-map",
        type=Path,
        default=None,
        help="image-owned test-impact map; defaults to the trusted root copy if present",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        candidate_sha, changed = inspect_candidate(args.candidate_root, args.base_sha)
        trusted_root = args.trusted_root.resolve(strict=True)
        # The map is read ONLY from the trusted image root, never the candidate,
        # so no candidate code or data influences the plan. Absent/defective ->
        # None -> unchanged fail-closed behavior.
        map_path = args.impact_map or (trusted_root / IMPACT_MAP_RELATIVE)
        impact_map = _load_impact_map(map_path) if map_path.is_file() else None
        plan = plan_selection(
            changed,
            trusted_root=trusted_root,
            assembly_base_sha=args.base_sha,
            candidate_sha=candidate_sha,
            trusted_source_revision=_load_trusted_revision(args.trusted_manifest),
            impact_map=impact_map,
        )
        _write(
            args.output,
            json.dumps(plan, sort_keys=True, separators=(",", ":")) + "\n",
        )
        _write(
            args.modes_output,
            "%s\n%s\n%s\n"
            % (
                plan["authoritative"]["mode"],
                plan["supplemental"]["mode"],
                plan["selection_mode"],
            ),
        )
        _write(
            args.authoritative_tests_output,
            "".join("%s\n" % item for item in plan["authoritative"]["tests"]),
        )
        return 0
    except (OSError, SelectionError, subprocess.SubprocessError) as exc:
        print("mac-certifier selector: %s" % exc, file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
