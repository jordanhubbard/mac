from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check-docs-accessibility.py"


def _module():
    spec = importlib.util.spec_from_file_location("mac_check_docs_accessibility", CHECKER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_documentation_links_and_images_validate_cleanly():
    result = subprocess.run(
        [sys.executable, str(CHECKER)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "links and images validated" in result.stdout


def test_mkdocs_nav_and_redirect_targets_resolve():
    module = _module()
    assert module.check_mkdocs_targets() == []


def test_missing_image_alt_text_is_reported(tmp_path, monkeypatch):
    module = _module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "page.md").write_text("# Title\n\n![](diagram.png)\n", encoding="utf-8")
    monkeypatch.setattr(module, "DOCS", docs)
    errors = module.check_links_and_images()
    assert any("missing alternative text" in error for error in errors)


def test_broken_relative_link_is_reported(tmp_path, monkeypatch):
    module = _module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "page.md").write_text("# Title\n\n[gone](does-not-exist.md)\n", encoding="utf-8")
    monkeypatch.setattr(module, "DOCS", docs)
    errors = module.check_links_and_images()
    assert any("broken relative link" in error for error in errors)


def test_links_inside_code_fences_are_ignored(tmp_path, monkeypatch):
    module = _module()
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "page.md").write_text(
        "# Title\n\n```text\n[not a link](missing.md)\n```\n", encoding="utf-8"
    )
    monkeypatch.setattr(module, "DOCS", docs)
    assert module.check_links_and_images() == []
