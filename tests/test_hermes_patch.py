from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess

import pytest

from mac.hermes_patch import apply_reviewed_patch


def _staged_patch(tmp_path: Path) -> tuple[Path, Path, Path]:
    stage = tmp_path / "stage"
    stage.mkdir()
    subprocess.run(["git", "init", "-q", str(stage)], check=True)
    subprocess.run(
        ["git", "-C", str(stage), "config", "user.email", "test@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(stage), "config", "user.name", "Test"], check=True)
    source = stage / "source.txt"
    source.write_text("old\n", encoding="utf-8")
    (stage / "other.py").write_text("ORIGINAL = True\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(stage), "add", "source.txt", "other.py"], check=True)
    subprocess.run(["git", "-C", str(stage), "commit", "-qm", "base"], check=True)
    head = subprocess.run(
        ["git", "-C", str(stage), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    patch = tmp_path / "change.patch"
    patch.write_text(
        "diff --git a/source.txt b/source.txt\n--- a/source.txt\n+++ b/source.txt\n@@ -1 +1 @@\n-old\n+new\n",
        encoding="utf-8",
    )
    digest = lambda value: hashlib.sha256(value).hexdigest()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "upstream_commit": head,
                "patch": patch.name,
                "patch_sha256": digest(patch.read_bytes()),
                "files": {
                    "source.txt": {
                        "original_sha256": digest(b"old\n"),
                        "patched_sha256": digest(b"new\n"),
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    return stage, source, manifest


def test_apply_reviewed_patch_is_hash_qualified_and_idempotent(tmp_path: Path) -> None:
    stage, _source, manifest = _staged_patch(tmp_path)
    assert apply_reviewed_patch(stage, manifest) == "patched"
    assert apply_reviewed_patch(stage, manifest) == "already_patched"


@pytest.mark.parametrize("already_patched", [False, True])
@pytest.mark.parametrize("change", ["unstaged", "staged", "untracked"])
def test_unrelated_source_is_rejected_before_patch_or_idempotent_acceptance(
    tmp_path: Path, already_patched: bool, change: str
) -> None:
    stage, source, manifest = _staged_patch(tmp_path)
    if already_patched:
        apply_reviewed_patch(stage, manifest)
    before = source.read_bytes()
    other = stage / ("extra.py" if change == "untracked" else "other.py")
    other.write_text("UNREVIEWED = True\n", encoding="utf-8")
    if change == "staged":
        subprocess.run(["git", "-C", str(stage), "add", other.name], check=True)
    with pytest.raises(RuntimeError, match="unrelated source modifications"):
        apply_reviewed_patch(stage, manifest)
    assert source.read_bytes() == before
