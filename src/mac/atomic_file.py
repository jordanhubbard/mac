"""Crash- and concurrency-safe file replacement.

Two distinct hazards are closed here, and both were live in this repository:

1. **A fixed temporary name in a shared directory is not private.** Writing
   ``<name>.tmp`` next to ``<name>`` is only atomic if exactly one process ever
   writes it. ``~/.mac`` is a per-*user* directory shared by every worker, CLI
   invocation and agent startup, and the AgentFS WebDAV share is written by
   many agents through a threaded server. Two writers that pick the same
   temporary path truncate and splice each other's bytes, and then one of them
   renames the mixture into place. ``os.replace`` is atomic; *choosing the same
   temporary path is not*. Every temporary here comes from
   :func:`tempfile.mkstemp`, so each writer owns a name nobody else can open.

2. **``os.replace`` is atomic with respect to other processes, not to a
   crash.** With delayed allocation (ext4 and friends) the rename can reach the
   journal before the data blocks do, so a power loss leaves a zero-length file
   where a complete one is expected. Readers in this codebase almost uniformly
   swallow parse errors and substitute an empty default, which turns that into
   silent data loss rather than a visible failure. So the data descriptor is
   fsynced before the rename and the parent directory is fsynced after it.

Modelled on the two writers that already got this right:
``read_only_report_verifier.atomic_write_result`` and
``deployment_attestation._atomic_private_json``.
"""

from __future__ import annotations

import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Any, Iterator, Optional

__all__ = [
    "atomic_write_bytes",
    "atomic_write_text",
    "atomic_writer",
    "fsync_directory",
]


def fsync_directory(directory: Path) -> None:
    """Durably record a rename in *directory* (best effort on odd platforms)."""

    try:
        directory_fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(directory_fd)
    except OSError:
        # Some filesystems (and some CI overlay mounts) refuse fsync on a
        # directory descriptor. The rename already happened; durability is the
        # only thing lost, so this must not fail the write.
        pass
    finally:
        os.close(directory_fd)


@contextmanager
def atomic_writer(
    path: Path,
    *,
    binary: bool = False,
    mode: Optional[int] = 0o600,
    encoding: Optional[str] = "utf-8",
) -> Iterator[IO[Any]]:
    """Yield a handle whose contents replace *path* atomically on clean exit.

    The temporary is uniquely named inside ``path.parent``, so concurrent
    writers to the same *path* cannot interleave: each writes its own file and
    the renames serialize, leaving one writer's bytes intact rather than a
    splice of both. On any exception the temporary is removed and *path* is
    left untouched.
    """

    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(dir=str(parent), prefix="." + path.name + ".", suffix=".tmp")
    temporary = Path(raw)
    try:
        try:
            if binary:
                stream: IO[Any] = os.fdopen(descriptor, "wb")
            else:
                stream = os.fdopen(descriptor, "w", encoding=encoding)
        except BaseException:
            os.close(descriptor)
            raise
        with stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_directory(parent)


def atomic_write_bytes(path: Path, data: bytes, *, mode: Optional[int] = 0o600) -> None:
    """Replace *path* with *data*, durably and without a shared temp name."""

    with atomic_writer(path, binary=True, mode=mode) as stream:
        stream.write(data)


def atomic_write_text(
    path: Path,
    text: str,
    *,
    mode: Optional[int] = 0o600,
    encoding: str = "utf-8",
) -> None:
    """Replace *path* with *text*, durably and without a shared temp name."""

    with atomic_writer(path, mode=mode, encoding=encoding) as stream:
        stream.write(text)
