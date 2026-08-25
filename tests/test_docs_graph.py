"""Tests for the docs-graph reachability gate (scripts/check-docs-graph.py).

The gate proves every current documentation file is reachable from README.md;
these tests prove the gate itself detects orphans, broken links, and inventory
omissions, and that the real tree passes.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-docs-graph.py"


def _module():
    spec = importlib.util.spec_from_file_location("mac_check_docs_graph", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_real_documentation_graph_is_fully_reachable():
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs-graph gate passed" in result.stdout


def _seed_common(module, tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    (docs / "reference").mkdir(parents=True)
    (docs / "archive").mkdir(parents=True)
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "DOCS", docs)
    monkeypatch.setattr(module, "README", tmp_path / "README.md")
    monkeypatch.setattr(module, "INVENTORY", docs / "reference" / "documentation-inventory.md")
    monkeypatch.setattr(module, "ARCHIVE_INDEX", docs / "archive" / "index.md")
    (docs / "archive" / "index.md").write_text("# Historical archive\n", encoding="utf-8")
    return docs


def test_orphaned_current_doc_is_reported(tmp_path, monkeypatch):
    module = _module()
    docs = _seed_common(module, tmp_path, monkeypatch)
    (tmp_path / "README.md").write_text(
        "# root\n\n[index](docs/reference/documentation-inventory.md)\n",
        encoding="utf-8",
    )
    (docs / "reference" / "documentation-inventory.md").write_text(
        "| Category | Source | Title |\n|---|---|---|\n"
        "| reference | [`reference/documentation-inventory.md`](../reference/documentation-inventory.md) | Inventory |\n"
        "| archive | [`archive/index.md`](../archive/index.md) | Archive |\n",
        encoding="utf-8",
    )
    (docs / "lonely.md").write_text("# lonely\n", encoding="utf-8")

    monkeypatch.setattr(
        module,
        "_tracked_docs",
        lambda: sorted(docs.rglob("*.md")),
    )
    errors = module.check()
    assert any("orphaned current doc" in e and "lonely.md" in e for e in errors)


def test_broken_internal_link_is_reported(tmp_path, monkeypatch):
    module = _module()
    docs = _seed_common(module, tmp_path, monkeypatch)
    (tmp_path / "README.md").write_text("# root\n\n[gone](docs/missing.md)\n", encoding="utf-8")
    (docs / "reference" / "documentation-inventory.md").write_text(
        "| Category | Source | Title |\n|---|---|---|\n", encoding="utf-8"
    )
    monkeypatch.setattr(module, "_tracked_docs", lambda: [])
    errors = module.check()
    assert any("broken internal link" in e for e in errors)


def test_doc_missing_from_inventory_is_reported(tmp_path, monkeypatch):
    module = _module()
    docs = _seed_common(module, tmp_path, monkeypatch)
    (tmp_path / "README.md").write_text("# root\n\n[page](docs/page.md)\n", encoding="utf-8")
    (docs / "page.md").write_text("# page\n", encoding="utf-8")
    (docs / "reference" / "documentation-inventory.md").write_text(
        "| Category | Source | Title |\n|---|---|---|\n", encoding="utf-8"
    )
    monkeypatch.setattr(module, "_tracked_docs", lambda: [docs / "page.md"])
    errors = module.check()
    assert any("missing from the documentation inventory" in e for e in errors)


def test_presentation_decks_are_allowlisted():
    module = _module()
    assert module._is_allowlisted(module.ROOT / "docs" / "presentation" / "x" / "README.md")
    assert not module._is_allowlisted(module.ROOT / "docs" / "getting-started.md")
