#!/usr/bin/env python3
"""Verify the AgentFabric overview document pair before it is shown to anyone.

Checks the two generated members against the invariants the specifications
require: the deck's slide count, speaker notes on every slide, a heading spine in
the narrative with no skipped levels, and a document-pair manifest that does not
claim publication authorization it does not have.

Exits non-zero with the failing invariant named. Writes an acceptance record to
$OBJ_DIR/agentfabric-overview/acceptance.json.
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

HERE = Path(__file__).resolve().parent
SOURCE = Path(os.environ.get("AGENTFABRIC_DECK_SOURCE", str(HERE)))
REPO = Path(os.environ.get("AGENTFABRIC_REPO", str(SOURCE.parents[2])))
OBJ = Path(os.environ.get("OBJ_DIR") or (REPO / "_build"))
PPTX = Path(os.environ.get("AGENTFABRIC_DECK_OUTPUT") or (SOURCE / "agentfabric-overview.pptx"))
DOCX = Path(
    os.environ.get("AGENTFABRIC_NARRATIVE_OUTPUT") or (SOURCE / "agentfabric-overview.docx")
)
MANIFEST = OBJ / "agentfabric-overview" / "document-pair-manifest.json"

EXPECTED_SLIDES = 20
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _deck_counts(path: Path) -> tuple[int, int, int]:
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        notes = sorted(
            name
            for name in archive.namelist()
            if name.startswith("ppt/notesSlides/notesSlide") and name.endswith(".xml")
        )
        with_text = 0
        for name in notes:
            root = ElementTree.fromstring(archive.read(name))
            if any((node.text or "").strip() for node in root.iter(f"{A}t")):
                with_text += 1
    return len(slides), len(notes), with_text


def _narrative_headings(path: Path) -> tuple[int, list[str]]:
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    levels: list[int] = []
    texts: list[str] = []
    for para in root.iter(f"{W}p"):
        style = para.find(f"{W}pPr/{W}pStyle")
        if style is None:
            continue
        value = style.attrib.get(f"{W}val", "").lower()
        if not value.startswith("heading"):
            continue
        digits = "".join(ch for ch in value if ch.isdigit())
        if not digits:
            continue
        levels.append(int(digits))
        texts.append("".join(node.text or "" for node in para.iter(f"{W}t")))
    skipped = [
        texts[index] for index in range(1, len(levels)) if levels[index] > levels[index - 1] + 1
    ]
    return len(levels), skipped


def main() -> int:
    failures: list[str] = []
    for path in (PPTX, DOCX, MANIFEST):
        if not path.is_file():
            failures.append(f"missing artifact: {path}")
    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1

    slides, notes, notes_with_text = _deck_counts(PPTX)
    headings, skipped = _narrative_headings(DOCX)
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    members = manifest.get("members", {})

    if slides != EXPECTED_SLIDES:
        failures.append(f"deck has {slides} slides, specification requires {EXPECTED_SLIDES}")
    if notes_with_text != slides:
        failures.append(f"{slides - notes_with_text} slide(s) carry no speaker notes")
    if headings < 40:
        failures.append(f"narrative has only {headings} headings; the spine looks truncated")
    if skipped:
        failures.append(f"narrative skips heading levels at: {skipped[:3]}")
    for name, member in members.items():
        if member.get("publication_authorized") and not member.get("published_location"):
            failures.append(f"{name} claims publication authorization with no published location")

    record = {
        "deck": {"slides": slides, "notes_slides": notes, "notes_with_text": notes_with_text},
        "narrative": {"headings": headings, "skipped_levels": skipped},
        "manifest": str(MANIFEST),
        "passed": not failures,
        "failures": failures,
    }
    out = OBJ / "agentfabric-overview" / "acceptance.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")

    for failure in failures:
        print(f"FAIL {failure}", file=sys.stderr)
    if failures:
        return 1
    print(
        f"pair verified: {slides} slides, notes on all of them, "
        f"{headings} narrative headings -> {out}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
