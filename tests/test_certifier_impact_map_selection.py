"""The certifier's optional impact-map precision layer.

The map may only turn a would-be full run into a focused one, and only when it
is fresh and the trusted-image copy of the changed file still matches the hash
recorded in the map. Every doubt falls back to the pre-map fail-closed plan.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


selector = _module("certifier_test_selector", ROOT / "deploy" / "certifier" / "select-tests.py")

BASE = "a" * 40
SOURCE = "src/mac/widget.py"
MAPPED_TEST = "tests/test_alpha.py"


def _trusted_root(tmp_path: Path) -> Path:
    root = tmp_path / "trusted"
    for relative in selector.INVARIANT_TESTS:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("def test_invariant(): pass\n", encoding="utf-8")
    (root / MAPPED_TEST).parent.mkdir(parents=True, exist_ok=True)
    (root / MAPPED_TEST).write_text("def test_a(): pass\n", encoding="utf-8")
    src = root / "src" / "mac" / "widget.py"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text("VALUE = 1\n", encoding="utf-8")
    return root


def _map_for(root: Path, *, base: str = BASE, correct_hash: bool = True) -> dict:
    raw = (root / "src" / "mac" / "widget.py").read_bytes()
    digest = "sha256:" + hashlib.sha256(raw if correct_hash else b"tampered").hexdigest()
    return {
        "schema": selector.IMPACT_MAP_SCHEMA,
        "base_sha": base,
        "nodeids": [f"{MAPPED_TEST}::test_a"],
        "file_tests": {SOURCE: [0]},
        "file_line_tests": {SOURCE: {"1": [0]}},
        "file_hashes": {SOURCE: digest},
        "always_run": [],
    }


def _plan(root: Path, impact_map):
    return selector.plan_selection(
        [SOURCE],
        trusted_root=root,
        assembly_base_sha=BASE,
        candidate_sha="b" * 40,
        trusted_source_revision="c" * 40,
        impact_map=impact_map,
    )


def test_without_map_unmapped_source_is_authoritative_full(tmp_path):
    root = _trusted_root(tmp_path)
    plan = _plan(root, None)
    assert plan["selection_mode"] == "authoritative_full"
    assert plan["authoritative"]["mode"] == "full"


def test_fresh_map_refines_full_to_focused(tmp_path):
    root = _trusted_root(tmp_path)
    plan = _plan(root, _map_for(root))
    assert plan["selection_mode"] == "source_focused"
    assert plan["authoritative"]["mode"] == "focused"
    assert MAPPED_TEST in plan["authoritative"]["tests"]
    for invariant in selector.INVARIANT_TESTS:
        assert invariant in plan["authoritative"]["tests"]
    assert plan["full_suite_count"] == 0


def test_stale_map_falls_back_to_full(tmp_path):
    root = _trusted_root(tmp_path)
    plan = _plan(root, _map_for(root, base="d" * 40))
    assert plan["selection_mode"] == "authoritative_full"


def test_hash_mismatch_falls_back_to_full(tmp_path):
    root = _trusted_root(tmp_path)
    plan = _plan(root, _map_for(root, correct_hash=False))
    assert plan["selection_mode"] == "authoritative_full"


def test_main_ignores_missing_trusted_map(tmp_path):
    # A trusted root without the map file must behave exactly as before.
    root = _trusted_root(tmp_path)
    loaded = selector._load_impact_map(root / selector.IMPACT_MAP_RELATIVE)
    assert loaded is None


def test_load_impact_map_rejects_wrong_schema(tmp_path):
    path = tmp_path / "m.json"
    path.write_text(json.dumps({"schema": "nope"}), encoding="utf-8")
    assert selector._load_impact_map(path) is None
