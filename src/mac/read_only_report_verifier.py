"""Authoritative verifier for read-only repository report tasks.

The report agent never supplies verification evidence.  This module runs from
the immutable MAC runtime in a second OpenShell sandbox and owns the complete
test lifecycle:

* prove the uploaded checkout is the registered exact base;
* watch tracked files and all Git controls while bootstrap/tests execute;
* quiesce every non-control process in the sandbox cgroup, including detached
  and double-forked descendants; and
* launch a fresh trusted process which re-proves the checkout and atomically
  writes the only result the host will accept.

Linux inotify is intentionally mandatory.  A final clean-tree check alone
cannot distinguish an honest test from ``modify; pass; restore``.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import fcntl
import hashlib
import json
import os
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from mac.repository_access_env import (
    fence_read_only_repository_environment,
    read_only_repository_content_digest,
)


RESULT_SCHEMA = "mac.sandbox_verification.v1"
INTEGRITY_SCHEMA = "mac.read_only_report_verification_integrity.v1"
RESULT_NAME = "mac-sandbox-verification.json"

_IN_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_IN_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_IN_ACCESS = 0x00000001
_IN_MODIFY = 0x00000002
_IN_ATTRIB = 0x00000004
_IN_CLOSE_WRITE = 0x00000008
_IN_MOVED_FROM = 0x00000040
_IN_MOVED_TO = 0x00000080
_IN_CREATE = 0x00000100
_IN_DELETE = 0x00000200
_IN_DELETE_SELF = 0x00000400
_IN_MOVE_SELF = 0x00000800
_IN_UNMOUNT = 0x00002000
_IN_Q_OVERFLOW = 0x00004000
_IN_IGNORED = 0x00008000
_IN_ONLYDIR = 0x01000000
_IN_DONT_FOLLOW = 0x02000000
_WATCH_MASK = (
    _IN_MODIFY
    | _IN_ATTRIB
    | _IN_CLOSE_WRITE
    | _IN_MOVED_FROM
    | _IN_MOVED_TO
    | _IN_CREATE
    | _IN_DELETE
    | _IN_DELETE_SELF
    | _IN_MOVE_SELF
    | _IN_UNMOUNT
    | _IN_Q_OVERFLOW
)
_EVENT = struct.Struct("iIII")


class VerificationError(RuntimeError):
    """A fail-closed verifier invariant was not satisfied."""


def _clip(value: Any, limit: int = 4000) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    head = limit // 4
    tail = limit - head
    return "%s\n... [%d chars omitted] ...\n%s" % (
        text[:head],
        len(text) - head - tail,
        text[-tail:],
    )


def _absolute_git() -> str:
    candidate = shutil.which("git", path="/usr/local/bin:/usr/bin:/bin")
    if not candidate:
        raise VerificationError("trusted Git executable is unavailable")
    resolved = Path(candidate).resolve(strict=True)
    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o022:
        raise VerificationError("trusted Git executable is not an immutable regular file")
    return str(resolved)


def _git_environment() -> dict[str, str]:
    environment: dict[str, str] = {}
    fence_read_only_repository_environment(environment)
    environment.update(
        {
            "HOME": "/tmp/mac-read-only-verifier-git-home",
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
    )
    return environment


def _git(worktree: Path, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
    root = worktree.resolve(strict=True)
    git_dir = root / ".git"
    info = git_dir.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise VerificationError("read-only verifier .git control is not a directory")
    return subprocess.run(
        [
            _absolute_git(),
            "--no-optional-locks",
            "--git-dir=%s" % git_dir,
            "--work-tree=%s" % root,
            "-c",
            "safe.directory=%s" % root,
            "-c",
            "core.worktree=%s" % root,
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.file.allow=never",
            *args,
        ],
        text=True,
        capture_output=True,
        check=False,
        env=_git_environment(),
    )


_GIT_CONTROL_PATHS = (
    "HEAD",
    "config",
    "config.worktree",
    "commondir",
    "gitdir",
    "index",
    "packed-refs",
    "shallow",
    "refs",
    "info",
    "objects/info",
    "worktrees",
    "modules",
)


def raw_git_control_digest(worktree: Path) -> str:
    """Hash Git controls through no-follow directory descriptors.

    This deliberately executes before Git in every trusted control process.
    Including the index is safe in the verifier lane because all Git commands
    run with optional locks disabled and no untrusted process survives the
    post-test quiescence boundary.
    """

    root = worktree.resolve(strict=True)
    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    digest = hashlib.sha256()

    def record(parent_fd: int, name: str, relative: str) -> None:
        relative_bytes = relative.encode("utf-8", "surrogateescape")
        try:
            info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            digest.update(b"M\0" + relative_bytes + b"\0")
            return
        if stat.S_ISLNK(info.st_mode):
            payload = os.readlink(name, dir_fd=parent_fd).encode("utf-8", "surrogateescape")
            digest.update(b"L\0" + relative_bytes + b"\0")
            digest.update(hashlib.sha256(payload).digest())
            return
        if stat.S_ISREG(info.st_mode):
            fd = os.open(name, os.O_RDONLY | nofollow | cloexec, dir_fd=parent_fd)
            try:
                observed = os.fstat(fd)
                if not stat.S_ISREG(observed.st_mode):
                    raise VerificationError("Git control changed type while read")
                payload = hashlib.sha256()
                for chunk in iter(lambda: os.read(fd, 1024 * 1024), b""):
                    payload.update(chunk)
            finally:
                os.close(fd)
            digest.update(b"F\0" + relative_bytes + b"\0")
            digest.update(payload.digest())
            return
        if stat.S_ISDIR(info.st_mode):
            fd = os.open(
                name,
                os.O_RDONLY | directory | nofollow | cloexec,
                dir_fd=parent_fd,
            )
            try:
                digest.update(b"D\0" + relative_bytes + b"\0")
                for child in sorted(os.listdir(fd)):
                    record(fd, child, "%s/%s" % (relative, child))
            finally:
                os.close(fd)
            return
        digest.update(b"O\0" + relative_bytes + b"\0")

    root_fd = os.open(root, os.O_RDONLY | directory | nofollow | cloexec)
    try:
        git_info = os.stat(".git", dir_fd=root_fd, follow_symlinks=False)
        if not stat.S_ISDIR(git_info.st_mode):
            raise VerificationError("read-only verifier .git is not a directory")
        git_fd = os.open(
            ".git",
            os.O_RDONLY | directory | nofollow | cloexec,
            dir_fd=root_fd,
        )
        try:
            digest.update(b"D\0.git\0")
            for relative in _GIT_CONTROL_PATHS:
                parts = relative.split("/")
                parent_fd = os.dup(git_fd)
                try:
                    traversed: list[str] = []
                    for part in parts[:-1]:
                        traversed.append(part)
                        try:
                            child_fd = os.open(
                                part,
                                os.O_RDONLY | directory | nofollow | cloexec,
                                dir_fd=parent_fd,
                            )
                        except OSError:
                            record(parent_fd, part, "/".join(traversed))
                            digest.update(b"M\0" + relative.encode() + b"\0")
                            break
                        os.close(parent_fd)
                        parent_fd = child_fd
                    else:
                        record(parent_fd, parts[-1], relative)
                finally:
                    os.close(parent_fd)
        finally:
            os.close(git_fd)
    finally:
        os.close(root_fd)
    return digest.hexdigest()


def tracked_paths(worktree: Path) -> set[str]:
    result = _git(worktree, ["ls-files", "-z", "--cached"])
    if result.returncode != 0:
        raise VerificationError("could not enumerate tracked verifier inputs")
    return {item for item in result.stdout.split("\0") if item}


def exact_identity(worktree: Path) -> dict[str, str]:
    """Read the exact repository identity with raw controls first."""

    controls = raw_git_control_digest(worktree)
    commands = {
        "head": ["rev-parse", "HEAD"],
        "tree": ["rev-parse", "HEAD^{tree}"],
        "refs": ["for-each-ref", "--format=%(refname) %(objectname)"],
        "status": ["status", "--porcelain", "--untracked-files=all"],
        "remotes": ["remote"],
    }
    observed: dict[str, str] = {"git_control_digest": controls}
    for name, args in commands.items():
        result = _git(worktree, args)
        if result.returncode != 0:
            raise VerificationError("trusted Git %s failed: %s" % (name, _clip(result.stderr, 500)))
        observed[name] = result.stdout
    observed["refs_digest"] = hashlib.sha256(observed["refs"].encode("utf-8")).hexdigest()
    observed["content_digest"] = read_only_repository_content_digest(worktree)
    return observed


def identity_problems(observed: Mapping[str, str], expected: Mapping[str, str]) -> list[str]:
    problems: list[str] = []
    comparisons = (
        ("git_control_digest", "Git controls"),
        ("head", "HEAD"),
        ("tree", "tree"),
        ("refs_digest", "refs"),
        ("content_digest", "repository content"),
    )
    for key, label in comparisons:
        if observed.get(key, "").strip() != expected.get(key, "").strip():
            problems.append("%s differs from the registered exact base" % label)
    if observed.get("status", "").strip():
        problems.append("repository status is not clean")
    if observed.get("remotes", "").strip():
        problems.append("verifier checkout contains a Git remote")
    return problems


class ProtectedInputMonitor:
    """Latch every transient mutation to tracked content or Git metadata."""

    def __init__(self, workspace: Path, worktree: Path, tracked: Iterable[str]):
        if not sys.platform.startswith("linux"):
            raise VerificationError("authoritative verifier requires Linux inotify")
        self.workspace = workspace.resolve(strict=True)
        self.worktree = worktree.resolve(strict=True)
        self.worktree_relative = self.worktree.relative_to(self.workspace).as_posix()
        self.tracked = {PurePosixPath(item).as_posix() for item in tracked}
        libc = ctypes.CDLL(None, use_errno=True)
        init = libc.inotify_init1
        init.argtypes = [ctypes.c_int]
        init.restype = ctypes.c_int
        fd = init(_IN_NONBLOCK | _IN_CLOEXEC)
        if fd < 0:
            error = ctypes.get_errno()
            raise VerificationError("inotify_init1 failed: %s" % os.strerror(error))
        self.fd = fd
        self._add = libc.inotify_add_watch
        self._add.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        self._add.restype = ctypes.c_int
        self._watches: dict[int, tuple[str, bool]] = {}
        try:
            self._install()
        except Exception:
            os.close(self.fd)
            raise

    def _watch(self, path: Path, relative: str, *, protected_file: bool = False) -> None:
        mask = _WATCH_MASK | _IN_DONT_FOLLOW
        if path.is_dir() and not path.is_symlink():
            mask |= _IN_ONLYDIR
        wd = self._add(self.fd, os.fsencode(path), mask)
        if wd < 0:
            error = ctypes.get_errno()
            raise VerificationError(
                "could not watch verifier input %s: %s" % (relative, os.strerror(error))
            )
        self._watches[wd] = (relative, protected_file)

    def _install(self) -> None:
        # Directory watches catch replacement/rename of tracked paths and writes
        # to expected-but-currently-absent names.  Individual inode watches catch
        # mutation through an attacker-created hard link whose event name would
        # otherwise appear to be an ignored output.
        for current, dirs, files in os.walk(self.workspace, topdown=True, followlinks=False):
            current_path = Path(current)
            relative = current_path.relative_to(self.workspace).as_posix()
            if relative == ".":
                relative = ""
            self._watch(current_path, relative)
            for name in files:
                path = current_path / name
                rel = path.relative_to(self.workspace).as_posix()
                if self._is_protected(rel):
                    self._watch(path, rel, protected_file=True)
            dirs[:] = [name for name in dirs if not (current_path / name).is_symlink()]

    def _is_protected(self, workspace_relative: str) -> bool:
        rel = PurePosixPath(workspace_relative).as_posix()
        repo = self.worktree_relative
        if rel == RESULT_NAME or rel == "%s/%s" % (repo, RESULT_NAME):
            return True
        if rel == repo or not rel.startswith(repo + "/"):
            # Task/control files outside the checkout are immutable after the
            # monitor starts.  Test output belongs inside the worktree.
            return True
        inner = rel[len(repo) + 1 :]
        if inner == ".git" or inner.startswith(".git/"):
            return True
        if inner in self.tracked:
            return True
        prefix = inner.rstrip("/") + "/"
        return any(item.startswith(prefix) for item in self.tracked)

    def drain(self, *, settle_seconds: float = 0.05) -> list[str]:
        deadline = time.monotonic() + max(0.0, settle_seconds)
        violations: list[str] = []
        while True:
            wait = max(0.0, deadline - time.monotonic())
            readable, _, _ = select.select([self.fd], [], [], wait)
            if not readable:
                break
            while True:
                try:
                    payload = os.read(self.fd, 1024 * 1024)
                except BlockingIOError:
                    break
                if not payload:
                    break
                offset = 0
                while offset + _EVENT.size <= len(payload):
                    wd, mask, _cookie, name_length = _EVENT.unpack_from(payload, offset)
                    offset += _EVENT.size
                    raw_name = payload[offset : offset + name_length]
                    offset += name_length
                    if mask & _IN_Q_OVERFLOW:
                        violations.append("inotify queue overflowed")
                        continue
                    base, protected_file = self._watches.get(wd, ("", True))
                    name = raw_name.rstrip(b"\0").decode("utf-8", "surrogateescape")
                    rel = "/".join(part for part in (base, name) if part)
                    if protected_file or self._is_protected(rel):
                        violations.append(
                            "protected input mutation observed: %s (mask=0x%x)" % (rel or ".", mask)
                        )
            deadline = time.monotonic() + max(0.0, settle_seconds)
        return sorted(set(violations))

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1


def _proc_cgroup(pid: int, proc_root: Path = Path("/proc")) -> tuple[str, ...]:
    try:
        lines = (proc_root / str(pid) / "cgroup").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ()
    return tuple(sorted(line.split(":", 2)[-1] for line in lines if ":" in line))


def _proc_uid_and_parent(pid: int, proc_root: Path = Path("/proc")) -> tuple[int, int] | None:
    try:
        lines = (proc_root / str(pid) / "status").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return None
    uid: Optional[int] = None
    parent: Optional[int] = None
    for line in lines:
        if line.startswith("Uid:"):
            uid = int(line.split()[1])
        elif line.startswith("PPid:"):
            parent = int(line.split()[1])
    return (uid, parent) if uid is not None and parent is not None else None


def _ancestor_pids(pid: int, proc_root: Path = Path("/proc")) -> set[int]:
    ancestors: set[int] = set()
    current = pid
    while current > 1 and current not in ancestors:
        ancestors.add(current)
        details = _proc_uid_and_parent(current, proc_root)
        if details is None:
            break
        current = details[1]
    if current > 0:
        ancestors.add(current)
    return ancestors


def sandbox_cgroup_candidates(
    *,
    current_pid: Optional[int] = None,
    proc_root: Path = Path("/proc"),
) -> list[int]:
    """Return every same-identity cgroup peer except trusted control ancestors."""

    pid = current_pid or os.getpid()
    own_cgroup = _proc_cgroup(pid, proc_root)
    if not own_cgroup:
        raise VerificationError("could not identify verifier sandbox cgroup")
    protected = _ancestor_pids(pid, proc_root)
    uid = os.getuid()
    candidates: list[int] = []
    try:
        entries = list(proc_root.iterdir())
    except OSError as exc:
        raise VerificationError("could not enumerate sandbox processes") from exc
    for entry in entries:
        if not entry.name.isdigit():
            continue
        other = int(entry.name)
        if other in protected:
            continue
        details = _proc_uid_and_parent(other, proc_root)
        if details is None or details[0] != uid:
            continue
        other_cgroup = _proc_cgroup(other, proc_root)
        same_sandbox = len(other_cgroup) == len(own_cgroup) and all(
            observed == expected
            or observed.startswith(expected.rstrip("/") + "/")
            or expected == "/"
            for observed, expected in zip(other_cgroup, own_cgroup)
        )
        if same_sandbox:
            candidates.append(other)
    return sorted(set(candidates))


def quiesce_sandbox_cgroup(
    *,
    candidate_provider: Callable[[], Sequence[int]] = sandbox_cgroup_candidates,
    kill_fn: Callable[[int, int], None] = os.kill,
    settle_seconds: float = 0.05,
    rounds: int = 20,
) -> None:
    """Kill and prove absence of all untrusted sandbox-cgroup processes.

    Process groups and sessions are irrelevant here: candidates are selected by
    cgroup membership.  Three consecutive empty scans establish a stable
    boundary before the fresh control process starts.
    """

    empty_scans = 0
    for _ in range(max(3, rounds)):
        candidates = [pid for pid in candidate_provider() if pid != os.getpid()]
        if not candidates:
            empty_scans += 1
            if empty_scans >= 3:
                return
            time.sleep(settle_seconds)
            continue
        empty_scans = 0
        for pid in candidates:
            try:
                kill_fn(pid, signal.SIGKILL)
            except ProcessLookupError:
                continue
            except PermissionError as exc:
                raise VerificationError(
                    "could not terminate sandbox cgroup process %d" % pid
                ) from exc
        time.sleep(settle_seconds)
    remaining = list(candidate_provider())
    if remaining:
        raise VerificationError(
            "sandbox cgroup did not quiesce; remaining pids: %s"
            % ",".join(str(pid) for pid in remaining[:20])
        )
    raise VerificationError("sandbox cgroup did not remain stably quiescent")


def _run_bounded(command: str, worktree: Path, timeout: float) -> dict[str, Any]:
    path_prefix = os.environ.get("MAC_SANDBOX_PATH_PREFIX", "")
    base_path = os.environ.get(
        "MAC_SANDBOX_BASE_PATH", "/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin"
    )
    prefix = 'export PATH="%s:%s"; hash -r 2>/dev/null || true; ' % (
        path_prefix.replace('"', '\\"'),
        base_path.replace('"', '\\"'),
    )
    environment = os.environ.copy()
    fence_read_only_repository_environment(environment)
    with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file:
        with tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file:
            started = time.time()
            process = subprocess.Popen(
                ["/bin/bash", "-lc", prefix + command],
                cwd=worktree,
                env=environment,
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
            )
            timed_out = False
            try:
                process.wait(timeout=max(1.0, timeout))
            except subprocess.TimeoutExpired:
                timed_out = True
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    process.kill()
                process.wait()
            else:
                with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                    os.killpg(process.pid, signal.SIGKILL)
            stdout_file.seek(0)
            stderr_file.seek(0)
            return {
                "command": command,
                "returncode": 124 if timed_out else int(process.returncode),
                "status": "fail" if timed_out or process.returncode else "pass",
                "stdout": _clip(stdout_file.read()),
                "stderr": _clip(stderr_file.read()),
                "duration_ms": int((time.time() - started) * 1000),
                **({"error": "command timed out after %ss" % timeout} if timed_out else {}),
            }


def _normalized_output_paths(raw: str) -> list[str]:
    outputs = [item.strip() for item in raw.splitlines() if item.strip()]
    normalized: list[str] = []
    for item in outputs:
        path = PurePosixPath(item)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
        ):
            raise VerificationError("unsafe declared verifier output path: %s" % item)
        text = path.as_posix()
        if text == ".git" or text.startswith(".git/"):
            raise VerificationError("declared verifier output overlaps Git controls")
        normalized.append(text)
    return sorted(set(normalized))


def _remove_nofollow(path: Path, root: Path) -> None:
    """Remove one explicit output without following any symlink component."""

    root = root.resolve(strict=True)
    relative = path.relative_to(root)
    current = root
    for part in relative.parts[:-1]:
        current = current / part
        info = current.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise VerificationError("declared output path traverses a non-directory")
    target = root / relative
    try:
        info = target.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        target.unlink()
        return
    for child in list(target.iterdir()):
        _remove_nofollow(child, root)
    target.rmdir()


def clean_allowed_outputs(worktree: Path, declared: Iterable[str]) -> None:
    """Clean Git-ignored outputs plus explicitly declared untracked outputs."""

    ignored = _git(worktree, ["clean", "-fdX"])
    if ignored.returncode != 0:
        raise VerificationError("could not clean Git-ignored verifier outputs")
    for relative in declared:
        tracked = _git(worktree, ["ls-files", "--", relative])
        if tracked.returncode != 0 or tracked.stdout.strip():
            raise VerificationError(
                "declared verifier output overlaps tracked content: %s" % relative
            )
        _remove_nofollow(worktree / relative, worktree)


def atomic_write_result(
    path: Path,
    payload: Mapping[str, Any],
    *,
    directory_fd: Optional[int] = None,
) -> None:
    """Atomically write through a no-follow directory descriptor."""

    directory = getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    dir_fd = (
        os.dup(directory_fd)
        if directory_fd is not None
        else os.open(
            path.parent.resolve(strict=True),
            os.O_RDONLY | directory | nofollow | cloexec,
        )
    )
    parent_info = os.fstat(dir_fd)
    if not stat.S_ISDIR(parent_info.st_mode):
        os.close(dir_fd)
        raise VerificationError("verification result parent is not a directory")
    temp_name = ".%s.control-%d-%s" % (path.name, os.getpid(), os.urandom(6).hex())
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            temp_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow | cloexec,
            0o600,
            dir_fd=dir_fd,
        )
        data = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode()
        view = memoryview(data)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise VerificationError("verification result temp is not a private regular file")
        os.close(descriptor)
        descriptor = None
        os.replace(temp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        os.fsync(dir_fd)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temp_name, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        os.close(dir_fd)


def _expected_from_environment() -> dict[str, str]:
    expected = {
        "head": os.environ.get("MAC_TASK_REPO_BASE_SHA", "").strip(),
        "tree": os.environ.get("MAC_TASK_REPO_BASE_TREE", "").strip(),
        "refs_digest": os.environ.get("MAC_TASK_REPO_REFS_DIGEST", "").strip(),
        "content_digest": os.environ.get("MAC_TASK_REPO_CONTENT_DIGEST", "").strip(),
        "git_control_digest": os.environ.get("MAC_TASK_REPO_GIT_CONTROL_DIGEST", "").strip(),
    }
    if not all(expected.values()):
        raise VerificationError("authoritative verifier exact-base context is incomplete")
    return expected


def revalidate_and_write(control: Mapping[str, Any]) -> int:
    workspace_fd = int(control.get("workspace_fd") or -1)
    worktree_fd = int(control.get("worktree_fd") or -1)
    if workspace_fd < 0 or worktree_fd < 0:
        raise VerificationError("trusted verifier directory descriptors are missing")
    if not stat.S_ISDIR(os.fstat(workspace_fd).st_mode) or not stat.S_ISDIR(
        os.fstat(worktree_fd).st_mode
    ):
        raise VerificationError("trusted verifier directory descriptor changed type")
    if Path("/proc/self/fd").is_dir():
        workspace = Path("/proc/self/fd/%d" % workspace_fd).resolve(strict=True)
        worktree = Path("/proc/self/fd/%d" % worktree_fd).resolve(strict=True)
    else:
        # Darwin's /dev/fd directory descriptors cannot be resolved through
        # pathlib, but F_GETPATH preserves the same descriptor-bound test seam.
        workspace = Path(
            fcntl.fcntl(workspace_fd, 50, b"\0" * 1024)
            .split(b"\0", 1)[0]
            .decode("utf-8", "surrogateescape")
        ).resolve(strict=True)
        worktree = Path(
            fcntl.fcntl(worktree_fd, 50, b"\0" * 1024)
            .split(b"\0", 1)[0]
            .decode("utf-8", "surrogateescape")
        ).resolve(strict=True)
    result_path = workspace / RESULT_NAME
    expected = {str(key): str(value) for key, value in dict(control.get("expected") or {}).items()}
    problems = [str(item) for item in control.get("problems") or []]
    try:
        # No Git may precede this raw comparison in the fresh process.
        raw_before_git = raw_git_control_digest(worktree)
        if raw_before_git != expected.get("git_control_digest"):
            problems.append("Git controls differ before trusted postcheck")
        clean_allowed_outputs(
            worktree, [str(item) for item in control.get("allowed_outputs") or []]
        )
        observed = exact_identity(worktree)
        problems.extend(identity_problems(observed, expected))
    except Exception as exc:  # noqa: BLE001 - every control error is evidence
        observed = {}
        problems.append("trusted postcheck failed: %s" % exc)
    test = dict(control.get("test") or {})
    bootstrap = control.get("bootstrap")
    command = str(test.get("command") or "")
    returncode = int(test.get("returncode") or 0)
    if not command:
        problems.append("repository contract test.command is missing")
        returncode = returncode or 1
    if returncode != 0:
        problems.append("repository contract test command failed")
    if isinstance(bootstrap, Mapping) and int(bootstrap.get("returncode") or 0) != 0:
        problems.append("repository bootstrap command failed")
        returncode = returncode or int(bootstrap.get("returncode") or 1)
    status = "fail" if problems else "pass"
    if status == "fail" and returncode == 0:
        returncode = 1
    mutation_problems = [str(item) for item in control.get("problems") or []]
    payload: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "status": status,
        "command": command,
        "returncode": returncode if status == "fail" else 0,
        "stdout": _clip(test.get("stdout")),
        "stderr": _clip(test.get("stderr")),
        "duration_ms": int(test.get("duration_ms") or 0),
        "worktree": str(worktree),
        "environment_delta": control.get("environment_delta") or {},
        "integrity": {
            "schema": INTEGRITY_SCHEMA,
            "immutable_inputs": not mutation_problems
            and observed.get("git_control_digest", "") == expected.get("git_control_digest", ""),
            "cgroup_quiescent": bool(control.get("cgroup_quiescent")),
            "fresh_control_process": True,
            "raw_git_control_first": True,
            "exact_base_revalidated": not identity_problems(observed, expected)
            if observed
            else False,
            "problems": sorted(set(problems)),
        },
    }
    if isinstance(bootstrap, Mapping):
        payload["bootstrap"] = dict(bootstrap)
    atomic_write_result(result_path, payload, directory_fd=workspace_fd)
    return 0 if status == "pass" else max(1, int(payload["returncode"] or 1))


def orchestrate() -> int:
    workspace = Path(os.environ.get("MAC_TASK_WORKSPACE") or os.getcwd()).resolve(strict=True)
    worktree = Path(os.environ.get("MAC_TASK_REPO_WORKTREE") or str(workspace)).resolve(strict=True)
    worktree.relative_to(workspace)
    expected = _expected_from_environment()
    command = os.environ.get("MAC_REPO_TEST_COMMAND", "").strip()
    bootstrap_command = os.environ.get("MAC_REPO_BOOTSTRAP_COMMAND", "").strip()
    bootstrap_creates_raw = os.environ.get("MAC_REPO_BOOTSTRAP_CREATES", "")
    declared_outputs = [item.strip() for item in bootstrap_creates_raw.splitlines() if item.strip()]
    allowed_outputs = _normalized_output_paths(bootstrap_creates_raw)
    timeout_raw = os.environ.get("MAC_WORKER_REPOSITORY_TEST_TIMEOUT", "1800")
    try:
        timeout = max(1.0, float(timeout_raw or "1800"))
    except ValueError:
        timeout = 1800.0

    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    workspace_fd = os.open(workspace, directory_flags)
    worktree_fd = os.open(worktree, directory_flags)
    initial = exact_identity(worktree)
    initial_problems = identity_problems(initial, expected)
    if initial_problems:
        os.close(worktree_fd)
        os.close(workspace_fd)
        raise VerificationError("; ".join(initial_problems))
    tracked = tracked_paths(worktree)
    monitor = ProtectedInputMonitor(workspace, worktree, tracked)
    bootstrap: Optional[dict[str, Any]] = None
    test: dict[str, Any]
    problems: list[str] = []
    try:
        if bootstrap_command:
            missing = [item for item in allowed_outputs if not (worktree / item).exists()]
            if missing or not declared_outputs:
                bootstrap = _run_bounded(bootstrap_command, worktree, timeout)
                bootstrap["creates"] = list(allowed_outputs)
                missing_after = [
                    item for item in declared_outputs if not (worktree / item).exists()
                ]
                if bootstrap.get("returncode") == 0 and missing_after:
                    bootstrap["returncode"] = 1
                    bootstrap["status"] = "fail"
                    bootstrap["missing_after"] = missing_after
                    bootstrap["error"] = "bootstrap command did not create declared outputs"
            else:
                bootstrap = {
                    "command": bootstrap_command,
                    "creates": list(allowed_outputs),
                    "returncode": 0,
                    "status": "skipped",
                    "reason": "declared bootstrap outputs already exist",
                }
            quiesce_sandbox_cgroup()
        if bootstrap is not None and int(bootstrap.get("returncode") or 0) != 0:
            test = {
                "command": command,
                "returncode": 1,
                "status": "fail",
                "stdout": "",
                "stderr": "repository bootstrap failed before verification tests",
            }
        elif not command:
            test = {
                "command": "",
                "returncode": 1,
                "status": "fail",
                "stdout": "",
                "stderr": "repository contract test.command is missing",
            }
        else:
            test = _run_bounded(command, worktree, timeout)
        quiesce_sandbox_cgroup()
        problems.extend(monitor.drain())
    finally:
        monitor.close()

    delta: dict[str, Any] = {}
    delta_path = Path(os.environ.get("MAC_TOOLCHAIN_ROOT", "")) / "environment-delta.json"
    try:
        loaded = json.loads(delta_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            delta = loaded
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    control = {
        "workspace": str(workspace),
        "worktree": str(worktree),
        "workspace_fd": workspace_fd,
        "worktree_fd": worktree_fd,
        "expected": expected,
        "allowed_outputs": allowed_outputs,
        "problems": problems,
        "bootstrap": bootstrap,
        "test": test,
        "environment_delta": delta,
        "cgroup_quiescent": True,
    }
    environment = {
        "HOME": "/tmp/mac-read-only-verifier-control-home",
        "PATH": "/opt/mac-venv/bin:/usr/local/bin:/usr/bin:/bin",
        "LC_ALL": "C",
        "PYTHONHASHSEED": "0",
    }
    try:
        completed = subprocess.run(
            [
                sys.executable,
                "-I",
                "-m",
                "mac.read_only_report_verifier",
                "--revalidate",
            ],
            input=json.dumps(control),
            text=True,
            capture_output=True,
            check=False,
            env=environment,
            cwd="/proc/self/fd/%d" % workspace_fd,
            pass_fds=(workspace_fd, worktree_fd),
        )
    finally:
        os.close(worktree_fd)
        os.close(workspace_fd)
    if completed.returncode != 0:
        sys.stderr.write(_clip(completed.stderr or completed.stdout) + "\n")
    return int(completed.returncode)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--revalidate", action="store_true")
    args = parser.parse_args(argv)
    if args.revalidate:
        try:
            loaded = json.load(sys.stdin)
            if not isinstance(loaded, dict):
                raise VerificationError("trusted revalidation control is not an object")
            return revalidate_and_write(loaded)
        except Exception as exc:  # noqa: BLE001 - fail closed at CLI boundary
            sys.stderr.write("read-only trusted revalidation failed: %s\n" % exc)
            return 70
    try:
        return orchestrate()
    except Exception as exc:  # noqa: BLE001 - fail closed at CLI boundary
        sys.stderr.write("read-only authoritative verifier failed: %s\n" % exc)
        return 70


if __name__ == "__main__":
    raise SystemExit(main())
