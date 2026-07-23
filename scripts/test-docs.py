#!/usr/bin/env python3
"""Validate and execute every shell fence in the production documentation book."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml


ROOT = Path(__file__).resolve().parents[1]
MKDOCS_CONFIG = ROOT / "mkdocs.yml"
SCHEMA = "mac.documentation_execution.v1"
CHAPTER_SCHEMA = "mac.docs.chapter.v1"
SHELL_LANGUAGES = {"bash", "sh", "shell"}
FORBIDDEN_ENV_MARKERS = (
    "API_KEY",
    "AUTH_TOKEN",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "PASSWORD",
    "SECRET",
    "SLACK_TOKEN",
)
FORBIDDEN_SCRIPT_PATTERNS = (
    re.compile(r"(?:^|\s)(?:rm\s+-rf|sudo\s+rm)(?:\s|$)"),
    re.compile(r"https?://(?!127\.0\.0\.1(?::\d+)?(?:/|$)|localhost(?::\d+)?(?:/|$)|example\.invalid(?:/|$))"),
    re.compile(r"(?:ghp_|github_pat_|sk-[A-Za-z0-9])"),
)


class DocumentationError(RuntimeError):
    pass


class _DocsYamlLoader(yaml.SafeLoader):
    pass


def _yaml_environment(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    values = loader.construct_sequence(node)
    key = str(values[0]) if values else ""
    fallback = str(values[1]) if len(values) > 1 else ""
    return os.environ.get(key, fallback)


_DocsYamlLoader.add_constructor("!ENV", _yaml_environment)


@dataclass(frozen=True)
class ShellBlock:
    path: Path
    line: int
    language: str
    source: str


@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    path: Path
    timeout_seconds: int
    blocks: tuple[ShellBlock, ...]


def _load_yaml(path: Path) -> dict[str, Any]:
    value = yaml.load(path.read_text(encoding="utf-8"), Loader=_DocsYamlLoader)
    if not isinstance(value, dict):
        raise DocumentationError(f"{path}: expected a YAML object")
    return value


def _book_paths() -> list[Path]:
    config = _load_yaml(MKDOCS_CONFIG)
    nav = config.get("nav")
    if not isinstance(nav, list):
        raise DocumentationError("mkdocs.yml: nav must be a list")
    book: Any = None
    for item in nav:
        if isinstance(item, dict) and "Book" in item:
            book = item["Book"]
            break
    if not isinstance(book, list):
        raise DocumentationError("mkdocs.yml: nav must contain a Book list")
    paths: list[Path] = []
    for entry in book:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise DocumentationError("mkdocs.yml: each Book entry must map one title to one path")
        relative = next(iter(entry.values()))
        if not isinstance(relative, str):
            raise DocumentationError("mkdocs.yml: Book paths must be strings")
        paths.append(ROOT / "docs" / relative)
    return paths


def _front_matter(path: Path, text: str) -> tuple[dict[str, Any], str, int]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DocumentationError(f"{path}: missing YAML front matter")
    try:
        end = next(index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---")
    except StopIteration as exc:
        raise DocumentationError(f"{path}: unterminated YAML front matter") from exc
    metadata = yaml.safe_load("\n".join(lines[1:end]))
    if not isinstance(metadata, dict):
        raise DocumentationError(f"{path}: front matter must be an object")
    return metadata, "\n".join(lines[end + 1 :]) + "\n", end + 2


def _shell_blocks(path: Path, body: str, body_start_line: int) -> tuple[ShellBlock, ...]:
    blocks: list[ShellBlock] = []
    opener: tuple[str, int, str, int] | None = None
    content: list[str] = []
    for offset, line in enumerate(body.splitlines(), start=body_start_line):
        if opener is None:
            match = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*([^ \t{]+)?(?:[ \t].*)?$", line)
            if not match:
                continue
            marker = match.group(1)
            language = str(match.group(2) or "").strip().lower()
            opener = (marker[0], len(marker), language, offset)
            content = []
            continue
        marker_char, marker_length, language, start_line = opener
        if re.match(rf"^[ \t]{{0,3}}{re.escape(marker_char)}{{{marker_length},}}[ \t]*$", line):
            if language in SHELL_LANGUAGES:
                blocks.append(
                    ShellBlock(path, start_line, language, "\n".join(content) + "\n")
                )
            opener = None
            content = []
            continue
        content.append(line)
    if opener is not None:
        raise DocumentationError(f"{path}:{opener[3]}: unterminated code fence")
    return tuple(blocks)


def load_chapters() -> list[Chapter]:
    chapters: list[Chapter] = []
    for expected, path in enumerate(_book_paths(), start=1):
        if not path.is_file():
            raise DocumentationError(f"missing book chapter: {path}")
        metadata, body, body_start = _front_matter(path, path.read_text(encoding="utf-8"))
        if metadata.get("schema") != CHAPTER_SCHEMA:
            raise DocumentationError(f"{path}: schema must be {CHAPTER_SCHEMA}")
        if metadata.get("chapter") != expected:
            raise DocumentationError(f"{path}: chapter must be {expected}")
        audiences = metadata.get("audiences")
        if not isinstance(audiences, list) or not audiences:
            raise DocumentationError(f"{path}: audiences must be a non-empty list")
        title = str(metadata.get("title") or "").strip()
        if not title:
            raise DocumentationError(f"{path}: title is required")
        timeout = int(metadata.get("timeout_seconds") or 60)
        if timeout < 1 or timeout > 300:
            raise DocumentationError(f"{path}: timeout_seconds must be between 1 and 300")
        blocks = _shell_blocks(path, body, body_start)
        if not blocks:
            raise DocumentationError(f"{path}: every chapter requires executable shell")
        for block in blocks:
            for pattern in FORBIDDEN_SCRIPT_PATTERNS:
                if pattern.search(block.source):
                    raise DocumentationError(
                        f"{path}:{block.line}: shell block violates the hermetic safety contract"
                    )
        chapters.append(Chapter(expected, title, path, timeout, blocks))
    if len(chapters) != 18:
        raise DocumentationError(f"book must contain exactly 18 chapters, found {len(chapters)}")
    book_paths = {chapter.path.resolve() for chapter in chapters}
    unmanaged: list[ShellBlock] = []
    for path in sorted((ROOT / "docs").rglob("*")):
        if path.suffix not in {".md", ".mdx"} or path.resolve() in book_paths:
            continue
        unmanaged.extend(_shell_blocks(path, path.read_text(encoding="utf-8"), 1))
    if unmanaged:
        sample = ", ".join(
            f"{block.path.relative_to(ROOT)}:{block.line}" for block in unmanaged[:8]
        )
        raise DocumentationError(
            "published shell fences outside the executable book are forbidden; "
            f"use console/text for non-executable transcripts ({sample})"
        )
    return chapters


def _clean_environment(workspace: Path) -> dict[str, str]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        docs_port = int(listener.getsockname()[1])
    path = os.pathsep.join(
        str(item)
        for item in (
            Path(sys.executable).parent,
            ROOT / ".venv" / "bin",
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            Path("/usr/bin"),
            Path("/bin"),
            Path("/usr/sbin"),
            Path("/sbin"),
        )
        if item.exists()
    )
    environment = {
        "HOME": str(workspace / "home"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "DOCS_DB": str(workspace / "mac.db"),
        "DOCS_HUB_URL": f"http://127.0.0.1:{docs_port}",
        "DOCS_PORT": str(docs_port),
        "DOCS_REPO": str(workspace / "sample-repo"),
        "DOCS_ROOT": str(ROOT),
        "MAC_HUB_TICK_INTERVAL_SECONDS": "0",
        "MAC_LEDGER_BACKUP_ENABLED": "0",
        "MAC_SECRET_KEY": "docs-only-secret-key-0123456789abcdef",
        "PATH": path,
        "PYTHONPATH": str(ROOT / "src"),
        "TMPDIR": str(workspace / "tmp"),
    }
    for key in os.environ:
        if any(marker in key.upper() for marker in FORBIDDEN_ENV_MARKERS):
            continue
        if key in {"TERM", "TZ"}:
            environment[key] = os.environ[key]
    return environment


def _chapter_script(chapter: Chapter) -> str:
    sections = ["set -euo pipefail"]
    for block in chapter.blocks:
        relative = block.path.relative_to(ROOT)
        sections.append(f"# {relative}:{block.line}")
        sections.append(block.source.rstrip())
    return "\n\n".join(sections) + "\n"


def _prepare_repository_fixture(workspace: Path) -> None:
    """Create the book's secret-free repository and reachable local origin."""

    repository = workspace / "sample-repo"
    remote = workspace / "sample-origin.git"
    for argv in (
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        ["git", "init", "--initial-branch=main", str(repository)],
        ["git", "-C", str(repository), "remote", "add", "origin", str(remote)],
    ):
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode != 0:
            raise DocumentationError(
                "could not prepare hermetic repository fixture: "
                f"{result.stderr.strip() or result.stdout.strip() or 'git failed'}"
            )


def execute_chapter(chapter: Chapter, *, verbose: bool = False) -> dict[str, Any]:
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix=f"mac-docs-{chapter.number:02d}-") as temporary:
        workspace = Path(temporary)
        for relative in ("home", "tmp", "work"):
            (workspace / relative).mkdir(parents=True, exist_ok=True)
        _prepare_repository_fixture(workspace)
        script = _chapter_script(chapter)
        result = subprocess.run(
            ["bash", "--noprofile", "--norc"],
            cwd=workspace / "work",
            env=_clean_environment(workspace),
            input=script,
            text=True,
            capture_output=True,
            timeout=chapter.timeout_seconds,
            check=False,
        )
    duration = time.monotonic() - started
    if result.returncode != 0:
        raise DocumentationError(
            f"chapter {chapter.number} failed with exit {result.returncode}\n"
            f"stdout:\n{result.stdout[-4000:]}\nstderr:\n{result.stderr[-4000:]}"
        )
    if verbose and (result.stdout or result.stderr):
        print(result.stdout, end="")
        print(result.stderr, end="")
    return {
        "chapter": chapter.number,
        "path": str(chapter.path.relative_to(ROOT)),
        "shell_block_count": len(chapter.blocks),
        "status": "pass",
        "duration_seconds": round(duration, 3),
        "script_sha256": hashlib.sha256(script.encode("utf-8")).hexdigest(),
    }


def _select(chapters: Iterable[Chapter], number: int | None) -> list[Chapter]:
    selected = [chapter for chapter in chapters if number is None or chapter.number == number]
    if not selected:
        raise DocumentationError(f"unknown chapter: {number}")
    return selected


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chapter", type=int)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--static-only", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    try:
        chapters = _select(load_chapters(), args.chapter)
        outcomes = []
        if not args.static_only:
            for chapter in chapters:
                outcome = execute_chapter(chapter, verbose=args.verbose)
                outcomes.append(outcome)
                print(
                    f"PASS chapter {chapter.number:02d}: {chapter.title} "
                    f"({len(chapter.blocks)} shell block(s))"
                )
        receipt = {
            "schema": SCHEMA,
            "status": "pass",
            "chapter_count": len(chapters),
            "shell_block_count": sum(len(chapter.blocks) for chapter in chapters),
            "outcomes": outcomes,
        }
        if args.receipt:
            args.receipt.parent.mkdir(parents=True, exist_ok=True)
            args.receipt.write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
        print(json.dumps({key: receipt[key] for key in ("schema", "status", "chapter_count", "shell_block_count")}, sort_keys=True))
        return 0
    except (DocumentationError, OSError, subprocess.TimeoutExpired, ValueError) as exc:
        print(f"documentation contract failed: {exc}", file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
