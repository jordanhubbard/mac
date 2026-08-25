from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from mac import repository_access_env
from mac import trusted_artifact


def test_regular_file_identity_rejects_relative_parent_and_final_symlinks(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="must be absolute"):
        trusted_artifact.nofollow_regular_file_identity("relative")

    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    (real_parent / "artifact").write_bytes(b"content")
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="parent is not a real directory"):
        trusted_artifact.nofollow_regular_file_identity(parent_link / "artifact")

    final_link = tmp_path / "final-link"
    final_link.symlink_to(real_parent / "artifact")
    with pytest.raises(ValueError, match="not a no-follow regular file"):
        trusted_artifact.nofollow_regular_file_identity(final_link)
    with pytest.raises(ValueError, match="not a no-follow regular file"):
        trusted_artifact.nofollow_regular_file_identity(real_parent)


def test_regular_file_identity_binds_open_descriptor(monkeypatch, tmp_path: Path) -> None:
    artifact = tmp_path / "artifact"
    artifact.write_bytes(b"content")
    original_fstat = trusted_artifact.os.fstat

    monkeypatch.setattr(
        trusted_artifact.os,
        "fstat",
        lambda descriptor: SimpleNamespace(st_mode=stat.S_IFDIR),
    )
    with pytest.raises(ValueError, match="descriptor is not a regular file"):
        trusted_artifact.nofollow_regular_file_identity(artifact)

    before = artifact.lstat()
    monkeypatch.setattr(
        trusted_artifact.os,
        "fstat",
        lambda descriptor: SimpleNamespace(
            st_mode=stat.S_IFREG,
            st_dev=before.st_dev,
            st_ino=before.st_ino + 1,
        ),
    )
    with pytest.raises(ValueError, match="changed while it was opened"):
        trusted_artifact.nofollow_regular_file_identity(artifact)
    monkeypatch.setattr(trusted_artifact.os, "fstat", original_fstat)

    absolute, digest = trusted_artifact.nofollow_regular_file_identity(artifact)
    assert absolute == str(artifact)
    assert digest.startswith("sha256:")


def test_source_bundle_rejects_invalid_empty_and_symlinked_inputs(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="absolute real directory"):
        trusted_artifact.nofollow_source_bundle_digest("relative", ())
    regular = tmp_path / "regular"
    regular.write_text("file", encoding="utf-8")
    with pytest.raises(ValueError, match="absolute real directory"):
        trusted_artifact.nofollow_source_bundle_digest(regular, ())
    root_link = tmp_path / "root-link"
    root_link.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="absolute real directory"):
        trusted_artifact.nofollow_source_bundle_digest(root_link, ())

    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "cache").mkdir()
    (empty / "cache" / "ignored.pyc").write_bytes(b"ignored")
    with pytest.raises(ValueError, match="bundle is empty"):
        trusted_artifact.nofollow_source_bundle_digest(empty, ("cache",))

    source = tmp_path / "source"
    source.mkdir()
    (source / "real.py").write_text("value = 1\n", encoding="utf-8")
    (source / "link.py").symlink_to(source / "real.py")
    with pytest.raises(ValueError, match="contains a symlink"):
        trusted_artifact.nofollow_source_bundle_digest(source, ("link.py",))


def test_source_bundle_skips_cache_and_hashes_files_deterministically(
    tmp_path: Path,
) -> None:
    root = tmp_path / "root"
    package = root / "src" / "mac"
    package.mkdir(parents=True)
    (package / "module.py").write_text("value = 1\n", encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "module.pyc").write_bytes(b"unstable")
    (root / "pyproject.toml").write_text("[project]\n", encoding="utf-8")

    first = trusted_artifact.nofollow_source_bundle_digest(root)
    (cache / "module.pyc").write_bytes(b"changed but ignored")
    second = trusted_artifact.nofollow_source_bundle_digest(root)

    assert first == second
    assert first[0] == str(root)
    assert first[1].startswith("sha256:")


def test_repository_content_digest_covers_links_regular_and_special_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".git" / "secret").write_text("ignored", encoding="utf-8")
    (root / "regular").write_text("content", encoding="utf-8")
    (root / "file-link").symlink_to("regular")
    target_dir = root / "target-dir"
    target_dir.mkdir()
    (target_dir / "child").write_text("child", encoding="utf-8")
    (root / "directory-link").symlink_to("target-dir", target_is_directory=True)
    fifo = root / "fifo"
    os.mkfifo(fifo)

    first = repository_access_env.read_only_repository_content_digest(root)
    (root / "file-link").unlink()
    (root / "file-link").symlink_to("target-dir/child")
    second = repository_access_env.read_only_repository_content_digest(root)

    assert first != second
    assert len(first) == 64
    assert stat.S_ISFIFO(fifo.lstat().st_mode)
