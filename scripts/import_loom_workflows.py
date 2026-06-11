"""One-off importer: convert loom workflow YAMLs to MAC's seed catalog.

Reads ~/Src/loom/workflows/defaults/*.yaml and writes per-workflow JSON
files to src/mac/data/workflows/. Each node's ``role_required`` is
normalised to the role *slug* (matching the agent_roles catalog) via
the persona_hint, with a label-to-slug fallback for edge cases.

Convenience tooling, not part of the runtime path.

Usage:
    .venv/bin/python scripts/import_loom_workflows.py [--loom PATH] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List

import yaml

DEFAULT_LOOM = Path.home() / "Src/loom/workflows/defaults"
DEFAULT_OUT = Path(__file__).resolve().parent.parent / "src/mac/data/workflows"

# Slugs that MAC's seed catalog drops (CEO/CFO/CTO). If a node points at
# one of these, we substitute the engineering-manager slug — the loom
# workflows we ship as defaults don't rely on the C-suite, but this
# guard keeps us honest if they ever do.
EXCLUDED_ROLE_SLUGS = {"ceo", "cfo", "cto"}
ROLE_FALLBACK = "engineering-manager"


def _slug_from_persona_hint(hint: Any, label: Any) -> str:
    if isinstance(hint, str) and "/" in hint:
        return hint.split("/", 1)[1].strip()
    if isinstance(label, str):
        return label.lower().replace(" ", "-").replace("_", "-")
    raise ValueError("cannot derive role slug from node %r / %r" % (hint, label))


def convert(yaml_text: str) -> Dict[str, Any]:
    raw = yaml.safe_load(yaml_text)
    if not isinstance(raw, dict):
        raise ValueError("workflow YAML must be a mapping")
    raw_nodes = raw.get("nodes") or []
    raw_edges = raw.get("edges") or []

    # Build the candidate node set, mapping persona_hint → slug. Nodes
    # whose role is in EXCLUDED_ROLE_SLUGS were previously rerouted to a
    # fallback, but in practice loom's exec roles are entry-point only
    # (no inbound edges from non-exec nodes). Drop them outright so the
    # MAC validator's "every non-start node must be reachable" invariant
    # holds without a fake-mapping.
    drop_keys: set = set()
    nodes = []
    for node in raw_nodes:
        slug = _slug_from_persona_hint(node.get("persona_hint"), node.get("role_required"))
        if slug in EXCLUDED_ROLE_SLUGS:
            drop_keys.add(node.get("node_key"))
            continue
        nodes.append(
            {
                "node_key": node.get("node_key"),
                "node_type": node.get("node_type", "task"),
                "role_required": slug,
                "persona_hint": node.get("persona_hint"),
                "max_attempts": int(node.get("max_attempts") or 1),
                "timeout_minutes": int(node.get("timeout_minutes") or 0),
                "instructions": (node.get("instructions") or "").strip(),
            }
        )

    edges = []
    for edge in raw_edges:
        from_key = edge.get("from_node_key") or ""
        to_key = edge.get("to_node_key") or ""
        if from_key in drop_keys or to_key in drop_keys:
            continue
        edges.append(
            {
                "from_node_key": from_key,
                "to_node_key": to_key,
                "condition": edge.get("condition", "success"),
                "priority": int(edge.get("priority") or 100),
            }
        )
    return {
        "slug": raw.get("id") or raw.get("workflow_type"),
        "name": raw.get("name") or raw.get("workflow_type"),
        "description": raw.get("description", ""),
        "workflow_type": raw.get("workflow_type"),
        "is_default": bool(raw.get("is_default", True)),
        "definition": {"nodes": nodes, "edges": edges},
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loom", type=Path, default=DEFAULT_LOOM)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args()

    if not args.loom.exists():
        raise SystemExit("loom workflows not found at %s" % args.loom)
    args.out.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for src in sorted(args.loom.glob("*.yaml")):
        payload = convert(src.read_text(encoding="utf-8"))
        # Stem with hyphens → underscores so the filename matches the
        # ``workflow_type`` (loom uses self-improvement / self_improvement).
        out_name = src.stem.replace("-", "_") + ".json"
        target = args.out / out_name
        target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        written.append(target)
    print("wrote %d workflows to %s" % (len(written), args.out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
