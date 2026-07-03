"""Tests for capturing generated media as durable task deliverables."""

from __future__ import annotations

import base64
import os
import time
from pathlib import Path

import pytest

from mac.worker import (
    _artifact_content_type,
    _durable_evidence_artifacts,
    _durable_media_artifacts,
)


def _write(path: Path, data: bytes, *, mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if mtime is not None:
        os.utime(path, (mtime, mtime))


@pytest.mark.parametrize(
    "name,expected",
    [
        ("x.png", "image/png"),
        ("x.jpg", "image/jpeg"),
        ("x.mp4", "video/mp4"),
        ("x.wav", "audio/wav"),
        ("x.bin", "application/octet-stream"),
        ("x.json", "application/json"),
    ],
)
def test_content_type(name, expected):
    assert _artifact_content_type(Path(name)) == expected


def _setup(tmp_path, monkeypatch):
    hermes = tmp_path / "hermes"
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    return hermes, task_dir


def test_captures_recent_generated_image(tmp_path, monkeypatch):
    hermes, task_dir = _setup(tmp_path, monkeypatch)
    png = b"\x89PNG\r\n\x1a\n" + b"generated-image-bytes"
    _write(hermes / "cache" / "images" / "gen1.png", png)  # mtime = now (recent)
    arts = _durable_media_artifacts(task_dir)
    assert len(arts) == 1
    a = arts[0]
    assert a["artifact_type"] == "media"
    assert a["content_type"] == "image/png"
    assert base64.b64decode(a["content_base64"]) == png
    assert a["name"] == "gen1.png"


def test_skips_stale_cache_media(tmp_path, monkeypatch):
    hermes, task_dir = _setup(tmp_path, monkeypatch)
    # A file from a prior task (mtime well before the task started) must not be
    # attached to this task's deliverables.
    _write(hermes / "cache" / "images" / "old.png", b"old",
           mtime=time.time() - 3600)
    _write(hermes / "cache" / "images" / "new.png", b"new")  # recent
    arts = _durable_media_artifacts(task_dir)
    names = {a["name"] for a in arts}
    assert names == {"new.png"}


def test_captures_audio_and_video(tmp_path, monkeypatch):
    hermes, task_dir = _setup(tmp_path, monkeypatch)
    _write(hermes / "cache" / "audio" / "speech.wav", b"RIFFwav")
    _write(hermes / "cache" / "video" / "clip.mp4", b"\x00\x00\x00 ftyp")
    arts = _durable_media_artifacts(task_dir)
    types = {a["content_type"] for a in arts}
    assert types == {"audio/wav", "video/mp4"}


def test_file_count_cap(tmp_path, monkeypatch):
    hermes, task_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_EVIDENCE_MEDIA_MAX_FILES", "2")
    for i in range(5):
        _write(hermes / "cache" / "images" / f"g{i}.png", b"x" * 10)
    arts = _durable_media_artifacts(task_dir)
    assert len(arts) == 2


def test_total_byte_cap(tmp_path, monkeypatch):
    hermes, task_dir = _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("MAC_EVIDENCE_MEDIA_TOTAL_MAX_BYTES", "10")
    _write(hermes / "cache" / "images" / "a.png", b"x" * 8)
    _write(hermes / "cache" / "images" / "b.png", b"y" * 8)
    arts = _durable_media_artifacts(task_dir)
    # First fits (8 <= 10); second would exceed the 10-byte total -> stop.
    assert sum(a["size_bytes"] for a in arts) <= 10


def test_no_cache_dir_is_safe(tmp_path, monkeypatch):
    _hermes, task_dir = _setup(tmp_path, monkeypatch)
    assert _durable_media_artifacts(task_dir) == []


def test_durable_evidence_merges_media(tmp_path, monkeypatch):
    hermes, task_dir = _setup(tmp_path, monkeypatch)
    (task_dir / "mac-evidence.json").write_text('{"ok": true}', encoding="utf-8")
    _write(hermes / "cache" / "images" / "gen.png", b"\x89PNG generated")
    result_path = task_dir / "result.txt"
    result_path.write_text("done", encoding="utf-8")
    arts = _durable_evidence_artifacts(task_dir, result_path)
    types = {a["artifact_type"] for a in arts}
    assert "media" in types and "verification_manifest" in types
