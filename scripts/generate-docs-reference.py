#!/usr/bin/env python3
"""Generate deterministic CLI and OpenAPI reference pages for the docs site."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mac.api import create_app  # noqa: E402
from mac.services import ControlPlane  # noqa: E402


CLI_OUTPUT = ROOT / "docs" / "reference" / "cli.md"
OPENAPI_OUTPUT = ROOT / "docs" / "reference" / "openapi.md"
INVENTORY_OUTPUT = ROOT / "docs" / "reference" / "documentation-inventory.md"
ARCHIVE_OUTPUT = ROOT / "docs" / "archive" / "index.md"


def _help(*parts: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "mac.cli", *parts, "--help"],
        cwd=ROOT,
        env={
            "PATH": os.environ.get("PATH", ""),
            # Editable environments may point at another worktree. Generated
            # help must always import the source beside this generator.
            "PYTHONPATH": str(ROOT / "src"),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError("mac %s --help failed" % " ".join(parts))
    return result.stdout.rstrip()


def _top_level_commands(root_help: str) -> tuple[str, ...]:
    match = re.search(r"^\s*\{([^}]+)\}\s*$", root_help, re.MULTILINE)
    if match is None:
        raise RuntimeError("could not discover top-level commands from mac --help")
    commands = tuple(part.strip() for part in match.group(1).split(",") if part.strip())
    if not commands:
        raise RuntimeError("mac --help exposed no top-level commands")
    return commands


def cli_reference() -> str:
    root_help = _help()
    commands = _top_level_commands(root_help)
    with ThreadPoolExecutor(max_workers=min(8, len(commands))) as executor:
        command_help = dict(zip(commands, executor.map(_help, commands)))
    sections = [
        "# Command-line reference",
        "",
        "This page is generated from the current parser. Do not edit it directly.",
        "The book uses executable `bash` blocks; reference usage is rendered as output.",
        "",
        "## mac",
        "",
        "```console",
        "$ mac --help",
        root_help,
        "```",
    ]
    for command in commands:
        sections.extend(
            (
                "",
                f"## mac {command}",
                "",
                "```console",
                f"$ mac {command} --help",
                command_help[command],
                "```",
            )
        )
    return "\n".join(sections) + "\n"


def openapi_reference() -> str:
    app = create_app(control_plane=ControlPlane.in_memory())
    schema = app.openapi()
    lines = [
        "# HTTP API reference",
        "",
        "This route index is generated from the current FastAPI OpenAPI schema.",
        "Use the schema exposed by a running hub at `/openapi.json` for complete",
        "request and response definitions.",
        "",
        "| Method | Path | Operation |",
        "|---|---|---|",
    ]
    for path, operations in sorted(schema.get("paths", {}).items()):
        for method, operation in sorted(operations.items()):
            if method.lower() not in {"delete", "get", "patch", "post", "put"}:
                continue
            summary = str(operation.get("summary") or operation.get("operationId") or "")
            lines.append(f"| `{method.upper()}` | `{path}` | {summary.replace('|', '&#124;')} |")
    return "\n".join(lines) + "\n"


def _title(path: Path) -> str:
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    match = re.search(r"^#\s+(.+?)\s*$", text, re.MULTILINE)
    return match.group(1).strip() if match else path.stem.replace("-", " ").title()


def _category(relative: Path) -> str:
    value = relative.as_posix()
    if value.startswith("book/"):
        return "book"
    if value.startswith("archive/") or value.startswith("superpowers/"):
        return "historical archive"
    if value.startswith("adr/"):
        return "architecture decision"
    if value.startswith("reference/"):
        return "generated reference"
    if any(token in value for token in ("runbook", "deployment", "onboarding", "cutover", "availability", "recovery")):
        return "runbook"
    if value == "index.md":
        return "landing page"
    return "supplemental reference"


def documentation_inventory() -> str:
    docs_root = ROOT / "docs"
    expected = {INVENTORY_OUTPUT, ARCHIVE_OUTPUT}
    pages = sorted(
        {
            path
            for path in docs_root.rglob("*")
            if path.suffix in {".md", ".mdx"}
        }
        | expected,
        key=lambda path: path.relative_to(docs_root).as_posix(),
    )
    lines = [
        "# Documentation inventory",
        "",
        "This generated inventory classifies every Markdown source included in the",
        "documentation tree. Book chapters are authoritative and executable. Runbooks",
        "and references describe production boundaries. Historical material is retained",
        "for provenance and is not a current operating contract.",
        "",
        "| Category | Source | Title |",
        "|---|---|---|",
    ]
    for page in pages:
        relative = page.relative_to(docs_root)
        title = _title(page).replace("|", "&#124;")
        lines.append(f"| {_category(relative)} | `{relative.as_posix()}` | {title} |")
    return "\n".join(lines) + "\n"


def archive_index() -> str:
    docs_root = ROOT / "docs"
    archived = sorted(
        [*(docs_root / "archive" / "field-notes").glob("*.md"), *(docs_root / "adr").glob("*.md")],
        key=lambda path: path.name,
    )
    lines = [
        "# Historical archive",
        "",
        "These field notes and architecture decisions explain how MAC reached its current",
        "contracts. They are evidence, not current instructions. Follow the numbered book",
        "and current runbooks for operational work. Old field-note URLs redirect here to",
        "their retained sources.",
        "",
        "## Archived records",
        "",
    ]
    for page in archived:
        relative = page.relative_to(docs_root)
        lines.append(f"- [{_title(page)}](../{relative.as_posix()}) — `{relative.as_posix()}`")
    return "\n".join(lines) + "\n"


def _check_or_write(path: Path, expected: str, *, write: bool) -> bool:
    if write:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(expected, encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")
        return True
    actual = path.read_text(encoding="utf-8") if path.exists() else ""
    if actual != expected:
        print(f"generated documentation is stale: {path.relative_to(ROOT)}", file=os.sys.stderr)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--write", action="store_true")
    mode.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    outputs = (
        (CLI_OUTPUT, cli_reference()),
        (OPENAPI_OUTPUT, openapi_reference()),
        (ARCHIVE_OUTPUT, archive_index()),
        (INVENTORY_OUTPUT, documentation_inventory()),
    )
    return 0 if all(_check_or_write(path, content, write=args.write) for path, content in outputs) else 1


if __name__ == "__main__":
    raise SystemExit(main())
