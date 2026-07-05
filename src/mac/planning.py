"""mac.planning – topology-ordering primitive for planning workflows.

Given a set of file/module paths from a repository that has a .codegraph
index, return an ordered list of *layers* sorted by import/call topology:

  leaf-first  (default) – leaves come first, core last.
    Use for migrations like C->Rust: translate the leaf utilities before
    the core modules that depend on them.

  core-first  (--core-first / order='core-first') – core comes first,
    leaves last.
    Use when the operator wants to understand the entry points first.

The algorithm:
  1. Open the .codegraph/codegraph.db SQLite database at repo_root.
  2. Load all "imports" edges between file nodes for the given paths.
  3. Run Kahn's topological sort: nodes with in-degree 0 form layer 0
     (leaves), the next wave is layer 1, etc.
  4. Files not present in the index are placed in a special "unknown"
     layer at the end (they have no dependency info).

Public API
----------
  order_layers(paths, repo_root, *, mode='leaf-first') -> OrderResult
  OrderResult.layers   – list of Layer objects (index = layer number)
  Layer.files          – list of file paths in this layer
  Layer.layer          – int layer index (0 = most-leaf)
  blast_radius(path, repo_root) -> list[str]  -- files that *depend on* path

CLI: mac plan order <paths...> [--repo <dir>] [--core-first] [--json]
"""

from __future__ import annotations

import json
import sqlite3
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

__all__ = [
    "OrderResult",
    "Layer",
    "order_layers",
    "blast_radius",
    "PLANNING_SCHEMA",
]

PLANNING_SCHEMA = "mac.planning.v1"


@dataclass
class Layer:
    """One level in the dependency topology."""

    layer: int
    files: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"layer": self.layer, "files": sorted(self.files)}


@dataclass
class OrderResult:
    """Result of order_layers()."""

    schema: str
    mode: str  # "leaf-first" | "core-first"
    repo_root: str
    layers: list[Layer]
    unknown: list[str]  # files not found in the codegraph index

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "mode": self.mode,
            "repo_root": self.repo_root,
            "layers": [layer.to_dict() for layer in self.layers],
            "unknown": sorted(self.unknown),
        }


def _normalize(path: str) -> str:
    """Strip leading ./ and trailing slashes; normalise backslashes."""
    p = str(path or "").strip().replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def _find_db(repo_root: Path) -> Optional[Path]:
    """Return the codegraph SQLite database path, or None if absent."""
    candidate = repo_root / ".codegraph" / "codegraph.db"
    return candidate if candidate.exists() else None


def _load_import_edges(
    db_path: Path,
    paths: list[str],
) -> tuple[dict[str, set[str]], set[str]]:
    """Load file->file import edges from the codegraph DB.

    Returns:
        deps:  mapping from file -> set of files it imports (direct deps)
        known: set of paths that exist in the DB
    """
    path_set = set(paths)
    deps: dict[str, set[str]] = {p: set() for p in path_set}
    known: set[str] = set()

    try:
        con = sqlite3.connect(str(db_path))
        try:
            cur = con.cursor()
            # Which of the requested paths are in the DB?
            cur.execute("SELECT path FROM files")
            for (db_path_str,) in cur.fetchall():
                normalized = _normalize(db_path_str)
                if normalized in path_set:
                    known.add(normalized)

            # Load all "imports" edges: file:X --(imports)--> <target node>
            # The target node lives in a specific file; follow it.
            cur.execute(
                """
                SELECT
                    e.source AS from_node,
                    n.file_path AS to_file
                FROM edges e
                JOIN nodes n ON e.target = n.id
                WHERE e.kind = 'imports'
                  AND e.source LIKE 'file:%'
                """
            )
            rows = cur.fetchall()
            for from_node, to_file_raw in rows:
                # from_node is like "file:core.py"
                from_file = _normalize(from_node[len("file:"):])
                to_file = _normalize(to_file_raw)
                if from_file in path_set and to_file in path_set and from_file != to_file:
                    deps[from_file].add(to_file)
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        pass

    return deps, known


def _topological_layers(
    paths: list[str],
    deps: dict[str, set[str]],
) -> list[list[str]]:
    """Kahn's algorithm; returns list-of-lists, layer 0 = most-leaf."""
    # in-degree = number of *other paths in the set* that this path imports.
    # A leaf (no imports from the set) has in-degree 0.
    in_degree: dict[str, int] = {p: 0 for p in paths}
    # reverse edges: who imports p?
    reverse: dict[str, list[str]] = {p: [] for p in paths}

    for p in paths:
        for dep in deps.get(p, set()):
            if dep in in_degree:
                in_degree[p] += 1
                reverse[dep].append(p)

    queue: deque[str] = deque(p for p in paths if in_degree[p] == 0)
    layers: list[list[str]] = []
    visited: set[str] = set()

    while queue:
        current_layer = sorted(queue)  # deterministic ordering within a layer
        layers.append(current_layer)
        visited.update(current_layer)
        queue.clear()
        for node in current_layer:
            for importer in reverse[node]:
                in_degree[importer] -= 1
                if in_degree[importer] == 0 and importer not in visited:
                    queue.append(importer)

    # Handle cycles: any unvisited node goes into a final "cycle" layer
    cycle_nodes = [p for p in paths if p not in visited]
    if cycle_nodes:
        layers.append(sorted(cycle_nodes))

    return layers


def order_layers(
    paths: Iterable[str],
    repo_root: str | Path = ".",
    *,
    mode: str = "leaf-first",
) -> OrderResult:
    """Compute topology-ordered layers for the given file paths.

    Parameters
    ----------
    paths:
        Relative or absolute file paths to order.  They must be *relative
        to repo_root* (or will be made relative) to match the codegraph index.
    repo_root:
        Root of the repository (where .codegraph/ lives).  Defaults to CWD.
    mode:
        ``"leaf-first"`` (default) – layer 0 = leaves, final layer = core.
        ``"core-first"``            – layer 0 = core, final layer = leaves.

    Returns
    -------
    OrderResult with ``.layers`` (Layer list) and ``.unknown`` (not in DB).
    """
    if mode not in ("leaf-first", "core-first"):
        raise ValueError("mode must be 'leaf-first' or 'core-first', got %r" % mode)

    repo_root_path = Path(repo_root).resolve()

    # Normalise all input paths to be relative to repo_root
    normalised: list[str] = []
    for p in paths:
        p_path = Path(p)
        if p_path.is_absolute():
            try:
                rel = p_path.relative_to(repo_root_path)
                normalised.append(_normalize(str(rel)))
            except ValueError:
                normalised.append(_normalize(p))
        else:
            normalised.append(_normalize(p))

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique_paths: list[str] = []
    for p in normalised:
        if p and p not in seen:
            seen.add(p)
            unique_paths.append(p)

    if not unique_paths:
        return OrderResult(
            schema=PLANNING_SCHEMA,
            mode=mode,
            repo_root=str(repo_root_path),
            layers=[],
            unknown=[],
        )

    db_path = _find_db(repo_root_path)
    if db_path is None:
        # No index: treat all files as independent leaves in one layer
        layer = Layer(layer=0, files=sorted(unique_paths))
        return OrderResult(
            schema=PLANNING_SCHEMA,
            mode=mode,
            repo_root=str(repo_root_path),
            layers=[layer],
            unknown=sorted(unique_paths),
        )

    deps, known = _load_import_edges(db_path, unique_paths)
    unknown = sorted(p for p in unique_paths if p not in known)
    indexed_paths = [p for p in unique_paths if p in known]

    raw_layers = _topological_layers(indexed_paths, deps)

    if mode == "core-first":
        raw_layers = list(reversed(raw_layers))

    layers = [Layer(layer=i, files=raw_layer) for i, raw_layer in enumerate(raw_layers)]

    return OrderResult(
        schema=PLANNING_SCHEMA,
        mode=mode,
        repo_root=str(repo_root_path),
        layers=layers,
        unknown=unknown,
    )


def blast_radius(
    path: str,
    repo_root: str | Path = ".",
) -> list[str]:
    """Return all files in the index that (transitively) depend on *path*.

    Useful for assessing the blast radius of changing a file: which files
    would need to be updated if the given file changes?

    Returns a sorted list of file paths relative to repo_root.
    """
    repo_root_path = Path(repo_root).resolve()
    norm_path = _normalize(path)
    db_path = _find_db(repo_root_path)
    if db_path is None:
        return []

    try:
        con = sqlite3.connect(str(db_path))
        try:
            cur = con.cursor()
            # Build full reverse import map: who imports whom (file level)
            cur.execute(
                """
                SELECT
                    n_from.file_path AS from_file,
                    n_to.file_path   AS to_file
                FROM edges e
                JOIN nodes n_from ON e.source = n_from.id
                JOIN nodes n_to   ON e.target = n_to.id
                WHERE e.kind = 'imports'
                  AND n_from.kind = 'file'
                """
            )
            # reverse_map: to_file -> set of from_files that import it
            reverse_map: dict[str, set[str]] = {}
            for from_file_raw, to_file_raw in cur.fetchall():
                from_file = _normalize(from_file_raw)
                to_file = _normalize(to_file_raw)
                if from_file != to_file:
                    reverse_map.setdefault(to_file, set()).add(from_file)
        finally:
            con.close()
    except (sqlite3.Error, OSError):
        return []

    # BFS from norm_path following reverse edges
    visited: set[str] = set()
    queue: deque[str] = deque([norm_path])
    while queue:
        current = queue.popleft()
        for importer in reverse_map.get(current, set()):
            if importer not in visited:
                visited.add(importer)
                queue.append(importer)

    # Exclude the starting node itself
    visited.discard(norm_path)
    return sorted(visited)
