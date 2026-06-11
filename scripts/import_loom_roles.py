"""One-off importer: convert loom SKILL.md personas to MAC's seed catalog.

Reads ~/Src/loom/personas/default/<slug>/SKILL.md, parses the YAML
frontmatter, captures the Markdown body as the system_prompt, and writes
the result to src/mac/data/roles/loom_seed.json.

This is convenience tooling, not part of the runtime path. Re-run when
loom adds/updates personas. The output is committed to the repo so MAC
can seed defaults without depending on the loom checkout at runtime.

Usage:
    .venv/bin/python scripts/import_loom_roles.py [--loom PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

import yaml

DEFAULT_LOOM = Path.home() / "Src/loom/personas/default"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/mac/data/roles/loom_seed.json"

# Lightweight capability inference per role-slug. Loom doesn't ship a
# canonical capabilities list (it relies on role match), so we seed
# something reasonable per role; operators can edit the JSON afterward.
EXCLUDE_SLUGS = {"ceo", "cfo", "cto"}

DEFAULT_CAPS: Dict[str, List[str]] = {
    "code-reviewer": ["review", "python"],
    "decision-maker": ["decision"],
    "devops-engineer": ["ops", "deploy", "ci"],
    "documentation-manager": ["docs"],
    "engineering-manager": ["leadership", "review"],
    "housekeeping-bot": ["ops", "cleanup"],
    "product-manager": ["product", "planning"],
    "project-manager": ["planning", "tracking"],
    "public-relations-manager": ["communication"],
    "qa-engineer": ["qa", "testing"],
    "remediation-specialist": ["ops", "debug"],
    "web-designer": ["design", "ux"],
    "web-designer-engineer": ["frontend", "design"],
}


FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_skill_md(path: Path) -> Tuple[Dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    match = FRONTMATTER_RE.match(text)
    if not match:
        raise ValueError("missing YAML frontmatter: %s" % path)
    header = yaml.safe_load(match.group(1)) or {}
    body = match.group(2).strip()
    return header, body


def role_row(slug: str, header: Dict[str, Any], body: str) -> Dict[str, Any]:
    meta = header.get("metadata") or {}
    description = header.get("description") or ""
    if isinstance(description, str):
        description = " ".join(description.split())
    return {
        "slug": slug,
        "name": (meta.get("role") or slug.replace("-", " ").title()).strip(),
        "display_name": meta.get("display_name"),
        "description": description.strip(),
        "system_prompt": body,
        "level": (meta.get("level") or "ic").strip().lower(),
        "reports_to": meta.get("reports_to"),
        "specialties": list(meta.get("specialties") or []),
        "default_capabilities": DEFAULT_CAPS.get(slug, []),
        "required_capabilities": [],
        "hardware_requirements": {},
        "metadata": {
            "source": "loom",
            "loom_version": str(meta.get("version") or "unknown"),
        },
    }


def build_catalog(loom_root: Path) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for slug_dir in sorted(loom_root.iterdir()):
        slug = slug_dir.name
        if slug in EXCLUDE_SLUGS:
            continue
        skill = slug_dir / "SKILL.md"
        if not slug_dir.is_dir() or not skill.exists():
            continue
        header, body = parse_skill_md(skill)
        row = role_row(slug, header, body)
        # `reports_to` references between roles must also stay inside our
        # catalog. Drop pointers at excluded roles so dangling refs don't
        # land in the seed.
        if row.get("reports_to") in EXCLUDE_SLUGS:
            row["reports_to"] = None
        rows.append(row)
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loom", type=Path, default=DEFAULT_LOOM)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.loom.exists():
        raise SystemExit("loom personas not found at %s" % args.loom)
    catalog = build_catalog(args.loom)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(catalog, indent=2) + "\n", encoding="utf-8")
    print("wrote %d roles to %s" % (len(catalog), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
