#!/usr/bin/env python3
"""Accessibility and link validation for the MAC documentation site.

This gate complements ``mkdocs build --strict`` (which fails on unknown nav or
broken *rendered* cross-references) with checks that are cheap to run without a
full site build and that also cover files not reachable through the nav:

* every relative Markdown link and image target resolves to a real file;
* every Markdown image carries non-empty alternative text; and
* every ``mkdocs.yml`` nav entry and ``redirects`` target resolves under
  ``docs/``.

Links inside fenced code blocks are transcripts, not navigation, so they are
ignored. URLs, in-page anchors, and ``mailto:`` links are out of scope for the
filesystem resolver. The check is deterministic and hermetic: it only reads the
repository tree.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable

import yaml

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
MKDOCS_CONFIG = ROOT / "mkdocs.yml"

_FENCE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"(!?)\[(?P<text>[^\]]*)\]\((?P<target>[^)\s]+)(?:\s+\"[^\"]*\")?\)")


class _EnvLoader(yaml.SafeLoader):
    pass


def _env(loader: yaml.SafeLoader, node: yaml.Node) -> str:
    values = loader.construct_sequence(node)
    return str(values[1]) if len(values) > 1 else ""


_EnvLoader.add_constructor("!ENV", _env)


def _strip_code_fences(text: str) -> str:
    lines = text.splitlines()
    kept: list[str] = []
    fence: str | None = None
    for line in lines:
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


def _iter_docs() -> Iterable[Path]:
    return sorted(DOCS.rglob("*.md"))


def _resolve(source: Path, target: str) -> tuple[Path, str]:
    clean = target.split("#", 1)[0].split("?", 1)[0]
    return (source.parent / clean).resolve(), clean


def _display(page: Path) -> str:
    for base in (ROOT, DOCS.parent, DOCS):
        try:
            return str(page.relative_to(base))
        except ValueError:
            continue
    return str(page)


def check_links_and_images() -> list[str]:
    errors: list[str] = []
    for page in _iter_docs():
        body = _strip_code_fences(page.read_text(encoding="utf-8"))
        for lineno, line in enumerate(body.splitlines(), start=1):
            for match in _LINK.finditer(line):
                is_image = match.group(1) == "!"
                text = match.group("text").strip()
                target = match.group("target").strip()
                rel = _display(page)
                if is_image and not text:
                    errors.append(f"{rel}:{lineno}: image is missing alternative text: {target}")
                if (
                    target.startswith("#")
                    or "://" in target
                    or target.startswith("mailto:")
                    or target.startswith("/")
                ):
                    # Anchors, URLs, mail links, and site-absolute paths are
                    # not resolved against the filesystem here.
                    continue
                resolved, clean = _resolve(page, target)
                if not clean:
                    continue
                if not resolved.exists():
                    errors.append(f"{rel}:{lineno}: broken relative link -> {target}")
    return errors


def _nav_targets(node: object) -> Iterable[str]:
    if isinstance(node, str):
        yield node
    elif isinstance(node, list):
        for item in node:
            yield from _nav_targets(item)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _nav_targets(value)


def check_mkdocs_targets() -> list[str]:
    errors: list[str] = []
    config = yaml.load(MKDOCS_CONFIG.read_text(encoding="utf-8"), Loader=_EnvLoader)
    for target in _nav_targets(config.get("nav", [])):
        if "://" in target:
            continue
        if not (DOCS / target).exists():
            errors.append(f"mkdocs.yml: nav target does not resolve under docs/: {target}")
    for plugin in config.get("plugins", []):
        if not isinstance(plugin, dict):
            continue
        redirects = plugin.get("redirects")
        if not isinstance(redirects, dict):
            continue
        for source, destination in (redirects.get("redirect_maps") or {}).items():
            if not (DOCS / source).exists() and not (DOCS / source).parent.exists():
                # the source is a legacy path that may be intentionally absent
                pass
            if not (DOCS / destination).exists():
                errors.append(
                    f"mkdocs.yml: redirect target does not resolve under docs/: {destination}"
                )
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    errors = check_links_and_images() + check_mkdocs_targets()
    if errors:
        for error in errors:
            print(f"documentation accessibility check failed: {error}", file=sys.stderr)
        return 1
    print(
        "documentation accessibility check passed: "
        f"{sum(1 for _ in _iter_docs())} pages, links and images validated"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
