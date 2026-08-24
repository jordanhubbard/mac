"""Contracts for the immutable trusted certifier harness inventory."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "certifier" / "harness_manifest.py"


def _load_manifest_module():
    spec = importlib.util.spec_from_file_location("certifier_harness_manifest", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_required_harness_file_exists_in_the_repository() -> None:
    """A stale required path must fail before the image-publication workflow."""
    manifest = _load_manifest_module()

    manifest._inventory(ROOT)
