#!/usr/bin/env python3
"""Build and verify the immutable MAC certifier harness manifest.

The external certifier must not trust tests or gate scripts supplied by the
candidate it is evaluating.  The image build records the reviewed baseline
suite in a content manifest.  At runtime this module rejects any candidate
whose harness differs from that baseline before candidate Python is imported.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
import sys
from pathlib import Path, PurePosixPath


SCHEMA = "mac.certifier_trusted_harness.v1"
MANAGED_TREES = ("tests",)
MANAGED_FILES = (
    "deploy/certifier/select-tests.py",
    "deploy/certifier/supplemental-contract-tests",
    "plugin/test_tools.py",
    "conftest.py",
    "pyproject.toml",
    "test-policy.toml",
    "scripts/run-contract-tests.sh",
    "scripts/coverage-policy.py",
    "scripts/test-portfolio.py",
)
FORBIDDEN_ROOT_CONTROLS = (
    ".coveragerc",
    "conftest.py",
    "pytest.ini",
    "setup.cfg",
    "sitecustomize.py",
    "tox.ini",
    "usercustomize.py",
)
IGNORED_PARTS = frozenset({".pytest_cache", "__pycache__"})
IGNORED_SUFFIXES = (".pyc", ".pyo")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class HarnessError(RuntimeError):
    """The trusted harness or candidate harness is not exact and regular."""


def _ignored(relative: PurePosixPath) -> bool:
    return bool(
        set(relative.parts) & IGNORED_PARTS
        or relative.name == ".DS_Store"
        or relative.name.endswith(IGNORED_SUFFIXES)
    )


def _regular_file(root: Path, relative: PurePosixPath) -> Path:
    path = root.joinpath(*relative.parts)
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise HarnessError(f"trusted harness file is missing: {relative}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise HarnessError(f"trusted harness path is not a regular file: {relative}")
    return path


def _inventory(root: Path) -> tuple[PurePosixPath, ...]:
    values: set[PurePosixPath] = set()
    for tree_name in MANAGED_TREES:
        tree = root / tree_name
        try:
            tree_metadata = tree.lstat()
        except OSError as exc:
            raise HarnessError(f"trusted harness tree is missing: {tree_name}") from exc
        if stat.S_ISLNK(tree_metadata.st_mode) or not stat.S_ISDIR(tree_metadata.st_mode):
            raise HarnessError(f"trusted harness tree is not a directory: {tree_name}")
        for path in tree.rglob("*"):
            relative = PurePosixPath(path.relative_to(root).as_posix())
            if _ignored(relative):
                continue
            metadata = path.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                raise HarnessError(f"trusted harness contains a symlink: {relative}")
            if stat.S_ISREG(metadata.st_mode):
                values.add(relative)
            elif not stat.S_ISDIR(metadata.st_mode):
                raise HarnessError(f"trusted harness contains a special file: {relative}")
    for name in MANAGED_FILES:
        relative = PurePosixPath(name)
        _regular_file(root, relative)
        values.add(relative)
    return tuple(sorted(values, key=str))


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def build_manifest(root: Path, *, source_revision: str) -> dict[str, object]:
    root = root.resolve(strict=True)
    if not SHA_RE.fullmatch(source_revision):
        raise HarnessError("source revision must be an exact lowercase Git SHA")
    files = {
        str(relative): _digest(_regular_file(root, relative))
        for relative in _inventory(root)
    }
    return {
        "schema": SCHEMA,
        "source_revision": source_revision,
        "files": files,
    }


def load_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise HarnessError("trusted harness manifest is unreadable") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema",
        "source_revision",
        "files",
    }:
        raise HarnessError("trusted harness manifest has an invalid shape")
    if payload["schema"] != SCHEMA:
        raise HarnessError("trusted harness manifest schema is invalid")
    if not isinstance(payload["source_revision"], str) or not SHA_RE.fullmatch(
        payload["source_revision"]
    ):
        raise HarnessError("trusted harness manifest source revision is invalid")
    files = payload["files"]
    if not isinstance(files, dict) or not files:
        raise HarnessError("trusted harness manifest file map is invalid")
    for raw_name, raw_digest in files.items():
        if not isinstance(raw_name, str) or not isinstance(raw_digest, str):
            raise HarnessError("trusted harness manifest entry is invalid")
        name = PurePosixPath(raw_name)
        if (
            name.is_absolute()
            or ".." in name.parts
            or str(name) != raw_name
            or not DIGEST_RE.fullmatch(raw_digest)
        ):
            raise HarnessError("trusted harness manifest entry is invalid")
    return payload


def verify_manifest(root: Path, manifest: dict[str, object]) -> None:
    root = root.resolve(strict=True)
    expected = manifest["files"]
    assert isinstance(expected, dict)
    observed_names = tuple(str(item) for item in _inventory(root))
    expected_names = tuple(sorted(expected))
    if observed_names != expected_names:
        missing = sorted(set(expected_names) - set(observed_names))
        extra = sorted(set(observed_names) - set(expected_names))
        details = []
        if missing:
            details.append("missing=" + ",".join(missing[:8]))
        if extra:
            details.append("extra=" + ",".join(extra[:8]))
        raise HarnessError("candidate harness inventory differs: " + " ".join(details))
    for raw_name in expected_names:
        relative = PurePosixPath(raw_name)
        observed = _digest(_regular_file(root, relative))
        if observed != expected[raw_name]:
            raise HarnessError(f"candidate harness digest differs: {raw_name}")
    for raw_name in FORBIDDEN_ROOT_CONTROLS:
        if raw_name in expected:
            continue
        path = root / raw_name
        if path.exists() or path.is_symlink():
            raise HarnessError(f"candidate added an untrusted test control: {raw_name}")


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    path.chmod(0o444)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="write a trusted baseline manifest")
    build.add_argument("--root", type=Path, required=True)
    build.add_argument("--source-revision", required=True)
    build.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify", help="verify a root against the baseline")
    verify.add_argument("--root", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build":
            _write_manifest(
                args.output,
                build_manifest(args.root, source_revision=args.source_revision),
            )
            return 0
        manifest = load_manifest(args.manifest)
        verify_manifest(args.root, manifest)
        print(
            "trusted harness verified: source=%s files=%d"
            % (manifest["source_revision"], len(manifest["files"]))
        )
        return 0
    except (HarnessError, OSError) as exc:
        print(f"mac-certifier: {exc}", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
