#!/usr/bin/env python3
"""Generate the durable MAC_* environment registry and operator reference.

The scanner only records literal MAC_* names in repository-owned runtime,
deployment, and automation sources.  It never reads environment values.  The
JSON output is the runtime source of truth consumed by ``mac.env_config``; the
Markdown output is an operator view generated from exactly the same records.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "src/mac/data/env_config_registry.json"
REFERENCE = ROOT / "docs/env-config-reference.md"
NAME_RE = re.compile(r"\bMAC_[A-Z][A-Z0-9_]*\b")
SOURCE_ROOTS = (ROOT / "src/mac", ROOT / "deploy", ROOT / "scripts")
SOURCE_SUFFIXES = {".py", ".sh", ".yaml", ".yml", ".toml", ".conf", ".service"}

FAMILIES = (
    ("MAC_SCIENTIFIC_OPTIMIZER_", "scientific-optimizer"),
    ("MAC_REPOSITORY_REF_RECONCILER_", "repository-lifecycle"),
    ("MAC_CODING_AGENT_", "coding-agent-auth"),
    ("MAC_CLIENT_PRINCIPALS_", "client-auth"),
    ("MAC_DEPLOY_ROUTER_", "deploy-router"),
    ("MAC_DEPLOY_AGENT_GEN_", "deploy-agent-generation"),
    ("MAC_DEPLOY_", "deployment"),
    ("MAC_REVIEW_", "review"),
    ("MAC_NOTIFIER_", "notifier"),
    ("MAC_EVIDENCE_", "evidence"),
    ("MAC_OPENSHELL_", "openshell-sandbox"),
    ("MAC_SANDBOX_", "openshell-sandbox"),
    ("MAC_WORKER_", "worker"),
    ("MAC_TASK_REPO_", "task-repository"),
    ("MAC_TASK_", "task-execution"),
    ("MAC_ROUTER_", "router"),
    ("MAC_HERMES_", "hermes-runtime"),
    ("MAC_OPENCLAW_", "openclaw-runtime"),
    ("MAC_FIRECRAWL_", "firecrawl-gateway"),
    ("MAC_QDRANT_", "qdrant-memory"),
    ("MAC_TOKENHUB_", "tokenhub-legacy"),
    ("MAC_WEBDAV_", "webdav-publish"),
    ("MAC_PUBLISH_", "publication"),
    ("MAC_MEMORY_", "memory"),
    ("MAC_RUNNER_", "kubernetes-runner"),
    ("MAC_ACP_", "acp"),
    ("MAC_AGENT_", "agent"),
    ("MAC_HUB_", "hub"),
    ("MAC_API_", "api-auth"),
    ("MAC_GITHUB_", "github-ingest"),
    ("MAC_BACKLOG_", "backlog-grooming"),
)

BOOL_MARKERS = (
    "_ENABLED", "_REQUIRED", "_ALLOW_", "_AUTO_", "_DRY_RUN",
    "_KEEP", "_REBUILD_", "_VALIDATE_", "_VERBOSE_", "_ROTATE_",
    "_REQUIRE_", "_UPLOAD_", "_RECONCILE_", "_REJECT_", "_PREFER_",
)
BOOL_SUFFIXES = ("_OK", "_GC", "_INSTALL", "_MANAGE", "_TRUSTED")
INT_SUFFIXES = (
    "_SECONDS", "_PORT", "_LIMIT", "_BYTES", "_DIM", "_ATTEMPTS",
    "_THRESHOLD", "_SIZE", "_MAX", "_TIMEOUT", "_INTERVAL",
    "_CONCURRENCY", "_TTL", "_FLOOR", "_AGE",
)
RETIRED = {"MAC_BEADS_BRIDGE_HUB_AGENT"}


def family_for(name: str) -> str:
    for prefix, family in FAMILIES:
        if name.startswith(prefix):
            return family
    return "core"


def type_for(name: str) -> str:
    if any(marker in name for marker in BOOL_MARKERS) or name.endswith(BOOL_SUFFIXES):
        return "bool"
    if name.endswith(INT_SUFFIXES):
        return "int"
    return "str"


def description_for(name: str, family: str, retired: bool) -> str:
    if retired:
        return "Retired beads bridge selector; ignored by current hub-agent resolution."
    words = name.removeprefix("MAC_").lower().replace("_", " ")
    return f"{family.replace('-', ' ').title()} setting: {words}."


def source_files() -> list[Path]:
    files: list[Path] = []
    for root in SOURCE_ROOTS:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix not in SOURCE_SUFFIXES:
                continue
            if path == Path(__file__) or "__pycache__" in path.parts:
                continue
            files.append(path)
    return sorted(files)


def build_records() -> list[dict[str, object]]:
    locations: dict[str, set[str]] = {}
    for path in source_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix == ".py":
            try:
                tree = ast.parse(text, filename=str(path))
                fragments = [
                    node.value
                    for node in ast.walk(tree)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)
                ]
            except SyntaxError:
                fragments = []
        else:
            fragments = [text]
        for name in {
            match
            for fragment in fragments
            for match in NAME_RE.findall(fragment)
        }:
            # Prefix fragments are template mechanics, not environment keys.
            if name.endswith("_"):
                continue
            locations.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    records = []
    for name in sorted(locations):
        family = family_for(name)
        retired = name in RETIRED
        records.append(
            {
                "name": name,
                "type": type_for(name),
                "default": None,
                "family": family,
                "description": description_for(name, family, retired),
                "retired": retired,
                "sources": sorted(locations[name]),
            }
        )
    return records


def render_reference(records: list[dict[str, object]]) -> str:
    lines = [
        "# MAC environment configuration reference",
        "",
        "Generated by `scripts/generate-env-config-registry.py`. Do not edit by hand.",
        "Defaults shown as `consumer-defined` are intentionally owned by the calling subsystem; the registry does not invent a second default.",
        "",
        "| Variable | Type | Default | Family | Description |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in records:
        default = item["default"] if item["default"] is not None else "consumer-defined"
        lines.append(
            "| `{name}` | {type} | {default} | {family} | {description} |".format(
                name=item["name"],
                type=item["type"],
                default=default,
                family=item["family"],
                description=item["description"],
            )
        )
    lines.extend(
        [
            "",
            "## Precedence and retirement",
            "",
            "Fallback precedence is left-to-right in `resolve_env_chain`. Fleet-scoped credential keys are resolved before legacy flat keys by their owning subsystem. `MAC_BEADS_BRIDGE_HUB_AGENT` is retained only as a documented retired name and is never consulted by `resolve_hub_agent`.",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail if generated files are stale")
    args = parser.parse_args()
    records = build_records()
    registry_text = json.dumps(records, indent=2, sort_keys=True) + "\n"
    reference_text = render_reference(records)
    if args.check:
        stale = []
        if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != registry_text:
            stale.append(str(OUTPUT.relative_to(ROOT)))
        if not REFERENCE.exists() or REFERENCE.read_text(encoding="utf-8") != reference_text:
            stale.append(str(REFERENCE.relative_to(ROOT)))
        if stale:
            raise SystemExit(
                "stale generated environment registry: %s; run %s"
                % (", ".join(stale), Path(__file__).relative_to(ROOT))
            )
        print(f"environment registry is current ({len(records)} variables)")
        return
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(registry_text, encoding="utf-8")
    REFERENCE.write_text(reference_text, encoding="utf-8")
    print(f"wrote {len(records)} variables to {OUTPUT.relative_to(ROOT)} and {REFERENCE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
