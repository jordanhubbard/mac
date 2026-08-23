#!/usr/bin/env python3
"""Docs-graph reachability gate for the MAC documentation set.

`scripts/check-docs-accessibility.py` proves every *link* resolves; this gate
proves the opposite direction — that every *current documentation file* is
**reachable** by following internal links out of the root `README.md`. A doc
that no link chain reaches is invisible to a reader who starts where readers
start, and it rots unseen. That is the failure this gate exists to catch.

What it enforces, all fail-closed:

* **No orphaned current docs.** Every tracked ``docs/**/*.md``/``*.mdx`` that is
  not an allowlisted generated file must be reachable from ``README.md`` through
  a chain of internal Markdown links. Leaf docs may be reached indirectly — the
  complete documentation index (``docs/reference/documentation-inventory.md``)
  is linked from ``README.md`` and links every current doc — so a direct README
  link is not required, only reachability.
* **No broken internal links** in any node the traversal visits.
* **Historical material stays behind the archive.** ADRs, field notes, and
  design specs must be reachable through the explicitly linked, visibly-labelled
  historical archive index (``docs/archive/index.md``); the gate confirms the
  archive index reaches each of them.
* **Nothing omitted from the inventory.** Every current doc must appear in the
  generated documentation inventory, so the index a reader trusts is complete.

Only files on the explicit allowlist below are exempt, each with a reason. The
check is deterministic and hermetic: it reads the tracked repository tree via
``git ls-files`` and never touches the network.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
README = ROOT / "README.md"
INVENTORY = DOCS / "reference" / "documentation-inventory.md"
ARCHIVE_INDEX = DOCS / "archive" / "index.md"

_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"(!?)\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")

# Current docs that are intentionally NOT required to be reachable from README,
# each with a reason. Keep this list short and justified: an allowlist is where
# a reachability gate quietly dies if it is allowed to grow without argument.
_ALLOWLIST: dict[str, str] = {
    # Pinned capability decks are point-in-time artifacts scoped to one commit,
    # never revised, and excluded from the inventory and the published site for
    # the same reason (see scripts/generate-docs-reference.py). They are not part
    # of the current-doc graph.
    "docs/presentation/": "pinned point-in-time capability decks, excluded from the site and inventory",
}


def _tracked_docs() -> list[Path]:
    out = subprocess.check_output(
        ["git", "ls-files", "docs/"], cwd=ROOT, text=True
    )
    return sorted(
        ROOT / line
        for line in out.splitlines()
        if line.endswith((".md", ".mdx"))
    )


def _is_allowlisted(path: Path) -> bool:
    rel = path.relative_to(ROOT).as_posix()
    return any(rel.startswith(prefix) for prefix in _ALLOWLIST)


def _strip_code_fences(text: str) -> str:
    kept: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        match = _FENCE.match(line)
        if fence is None:
            if match:
                fence = match.group(1)[0]
                kept.append("")
                continue
            kept.append(line)
        else:
            if match and match.group(1)[0] == fence:
                fence = None
            kept.append("")
    return "\n".join(kept)


def _internal_links(page: Path) -> list[tuple[str, Path]]:
    """Return (raw_target, resolved_path) for internal Markdown links in *page*.

    Images, URLs, in-page anchors, ``mailto:`` and site-absolute links are not
    navigation and are skipped. Targets are resolved relative to *page*.
    """
    if not page.exists():
        return []
    body = _strip_code_fences(page.read_text(encoding="utf-8", errors="replace"))
    links: list[tuple[str, Path]] = []
    for match in _LINK.finditer(body):
        if match.group(1) == "!":
            continue
        target = match.group("target").strip()
        if (
            not target
            or target.startswith("#")
            or "://" in target
            or target.startswith("mailto:")
            or target.startswith("/")
        ):
            continue
        clean = target.split("#", 1)[0].split("?", 1)[0]
        if not clean:
            continue
        resolved = (page.parent / clean).resolve()
        links.append((target, resolved))
    return links


def _traverse(start: Path) -> tuple[set[Path], list[str]]:
    """BFS the internal Markdown link graph from *start*.

    Returns the set of reachable Markdown files and a list of broken-link
    errors observed along the way.
    """
    seen: set[Path] = set()
    broken: list[str] = []
    stack = [start.resolve()]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        for target, resolved in _internal_links(current):
            if not resolved.exists():
                try:
                    where = current.relative_to(ROOT).as_posix()
                except ValueError:
                    where = str(current)
                broken.append(f"{where}: broken internal link -> {target}")
                continue
            if resolved.suffix in {".md", ".mdx"} and resolved not in seen:
                stack.append(resolved)
    return seen, broken


def _inventory_sources() -> set[str]:
    """Return the docs-relative source paths listed in the inventory table."""
    if not INVENTORY.exists():
        return set()
    sources: set[str] = set()
    row = re.compile(r"^\|[^|]*\|\s*\[?`(?P<src>[^`]+)`\]?")
    for line in INVENTORY.read_text(encoding="utf-8").splitlines():
        match = row.match(line)
        if match:
            sources.add(match.group("src").strip())
    return sources


def check() -> list[str]:
    errors: list[str] = []
    docs = [p for p in _tracked_docs() if not _is_allowlisted(p)]
    docs_set = {p.resolve() for p in docs}

    reachable, broken = _traverse(README)
    errors.extend(broken)

    orphans = sorted(
        p.relative_to(ROOT).as_posix() for p in docs_set if p not in reachable
    )
    for orphan in orphans:
        errors.append(
            f"orphaned current doc (not reachable from README.md): {orphan}"
        )

    # Historical material must be reachable specifically through the archive
    # index, not only via some incidental link, so it is quarantined and labelled.
    archive_reachable, _ = _traverse(ARCHIVE_INDEX)
    historical = [
        p
        for p in docs_set
        if p.relative_to(DOCS).as_posix().startswith(("archive/", "adr/", "superpowers/"))
    ]
    for page in sorted(historical, key=lambda p: p.relative_to(ROOT).as_posix()):
        if page not in archive_reachable and page != ARCHIVE_INDEX.resolve():
            errors.append(
                "historical doc not reachable from the archive index "
                f"({ARCHIVE_INDEX.relative_to(ROOT).as_posix()}): "
                f"{page.relative_to(ROOT).as_posix()}"
            )

    inventory_sources = _inventory_sources()
    for page in sorted(docs_set, key=lambda p: p.relative_to(ROOT).as_posix()):
        rel_docs = page.relative_to(DOCS).as_posix()
        if rel_docs not in inventory_sources:
            errors.append(
                "current doc missing from the documentation inventory "
                f"({INVENTORY.relative_to(ROOT).as_posix()}): "
                f"{page.relative_to(ROOT).as_posix()}"
            )

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    errors = check()
    if errors:
        for error in errors:
            print(f"docs-graph gate failed: {error}", file=sys.stderr)
        print(
            f"\n{len(errors)} problem(s). Every current doc must be reachable from "
            "README.md, free of broken links, and listed in "
            "docs/reference/documentation-inventory.md.",
            file=sys.stderr,
        )
        return 1
    reachable_docs = sum(
        1 for p in _tracked_docs() if not _is_allowlisted(p)
    )
    print(
        "docs-graph gate passed: "
        f"{reachable_docs} current docs reachable from README.md, "
        "no orphans, no broken links, inventory complete."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
