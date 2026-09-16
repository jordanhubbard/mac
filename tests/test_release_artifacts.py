from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"


def test_release_tags_publish_an_importable_version_matched_wheel() -> None:
    raw = WORKFLOW.read_text(encoding="utf-8")
    workflow = yaml.safe_load(raw)

    assert workflow[True]["push"]["tags"] == ["v[0-9]+.[0-9]+.[0-9]+"]
    assert workflow["permissions"]["contents"] == "write"
    assert "make package-cli" in raw
    assert '"mac-${version}-"*.whl' in raw
    shell = " ".join(raw.replace("\\\n", " ").split())
    assert "uv sync --locked --no-dev --no-install-project --extra postgres --extra relay" in shell
    assert 'uv pip install --no-deps --python /tmp/mac-release-smoke/bin/python "$wheel"' in raw
    assert "uv pip check --python /tmp/mac-release-smoke/bin/python" in raw
    assert "import mac, mac.task_executor" in raw
    assert 'gh release upload "$RELEASE_TAG" dist/mac-*.whl --clobber' in raw


def test_release_wheel_is_built_from_the_tag_checkout() -> None:
    raw = WORKFLOW.read_text(encoding="utf-8")
    checkout = raw.index("actions/checkout@")
    build = raw.index("make package-cli")
    upload = raw.index("gh release upload")

    assert checkout < build < upload
