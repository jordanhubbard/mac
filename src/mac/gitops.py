"""Git operations and remote-URL handling for the control plane.

Provides validation, host detection, credential redaction, and canonical
publication/freshness helpers used when the control plane inspects or updates git
remotes, refs, and pull requests.
"""

from __future__ import annotations

import fcntl
import functools
import hashlib
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Dict, Optional, Tuple
import sys
from urllib.parse import quote as _quote, urlparse, urlsplit, urlunsplit


@dataclass
class PullRequestResult:
    host: str
    number: int
    url: str
    state: str


@dataclass
class PullRequestMergeResult:
    """Outcome of asking a forge to merge a pull request.

    ``merged`` is the only success signal.  ``blocked`` distinguishes "the
    forge refused because its own gates have not passed yet" (retry later,
    the PR is fine) from a hard error (raised, never returned).

    ``serialization`` names WHICH landing mechanism produced this outcome, so
    the guarantee behind a merge is recorded rather than assumed:

    ``merge_queue``
        The forge's merge queue owns the landing.  It builds a speculative
        merge candidate, tests the projected post-merge tree, and merges in
        order — the property :mod:`mac.merge_queue` models (bors' "Not Rocket
        Science Rule").  The tree that was tested IS the tree that lands.
    ``direct_squash``
        A plain squash merge.  Required status checks ran against a merge
        candidate built from *some* canonical tip; nothing stops the canonical
        branch from advancing between the checks finishing and the merge
        executing, so the landed tree may never have been tested as such.
        Callers must re-validate the canonical tip immediately before asking
        for this merge; ``queued`` is False and the evidence says so.

    ``queued`` is True when the PR was accepted into the merge queue but has
    not landed yet: not a failure, and not yet a success.
    """

    merged: bool
    number: int
    sha: str = ""
    blocked: bool = False
    reason: str = ""
    serialization: str = ""
    queued: bool = False


_GIT_REMOTE_URL_RE = re.compile(
    r"^(?:https?://|ssh://|git://|file://|git@|/)[A-Za-z0-9._\-:/@%+~?=&]*$"
)
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
_GIT_TIMEOUT_STATE = threading.local()


def validate_git_remote_url(value: str) -> str:
    """Validate a remote URL before it is passed to git as an argument."""
    if not value or value.startswith("-"):
        raise ValueError("git remote URL is empty or looks like a flag: %r" % value)
    if len(value) > 2048:
        raise ValueError("git remote URL exceeds 2048 byte limit")
    if not _GIT_REMOTE_URL_RE.match(value):
        raise ValueError("git remote URL does not match a recognised scheme: %r" % value)
    if value.startswith(("https://", "http://")) and urlsplit(value).username:
        raise ValueError(
            "git remote URL must not embed credentials; use environment-backed authentication"
        )
    return value


def validate_git_ref(value: str) -> str:
    """Validate a branch/ref component before it is passed to git."""
    if not value or value.startswith("-"):
        raise ValueError("git ref is empty or looks like a flag: %r" % value)
    if len(value) > 512:
        raise ValueError("git ref exceeds 512 byte limit")
    if not _GIT_REF_RE.match(value):
        raise ValueError("git ref contains disallowed characters: %r" % value)
    if (
        value.startswith("/")
        or value.endswith(("/", "."))
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(part.endswith(".lock") for part in value.split("/"))
    ):
        raise ValueError("git ref has an invalid shape: %r" % value)
    return value


@dataclass(frozen=True)
class CanonicalPublicationTarget:
    """The exact canonical source and task destination used by a guarded push."""

    worktree: Path
    canonical_remote_url: str = dataclass_field(repr=False)
    remote: str = dataclass_field(repr=False)
    remote_display: str
    canonical_branch: str
    destination_branch: str
    prepared_base_sha: str
    task_head_sha: str
    isolated_ref: str
    git_common_dir: Path
    lock_path: Path


@dataclass(frozen=True)
class CanonicalFreshnessResult:
    ok: bool
    target: Optional[CanonicalPublicationTarget]
    head_sha: str = ""
    canonical_tip_sha: str = ""
    files_changed: Tuple[str, ...] = ()
    error: str = ""
    push_returncode: Optional[int] = None
    push_stdout: str = ""
    push_stderr: str = ""
    remote_verified: bool = False

    def evidence(self) -> dict[str, object]:
        target = self.target
        return {
            "ok": self.ok,
            "remote": target.remote_display if target else "",
            "canonical_branch": target.canonical_branch if target else "",
            "prepared_base_sha": target.prepared_base_sha if target else "",
            "canonical_tip_sha": self.canonical_tip_sha,
            "task_head_sha": self.head_sha or (target.task_head_sha if target else ""),
            "isolated_ref": target.isolated_ref if target else "",
            "ancestry_valid": self.ok,
            "error": self.error,
        }


def detect_host(repo_url: str) -> str:
    """Return ``"github"`` or ``"gitea"`` for a git remote URL.

    Matches the host predicate in :func:`inject_git_remote_auth` so
    both helpers agree on what to call which host.
    """
    parsed = urlparse(repo_url if "://" in repo_url else "https://" + repo_url)
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValueError("could not parse host from repo URL: %s" % repo_url)
    if host in {"github.com", "api.github.com"} or host.endswith(".github.com"):
        return "github"
    return "gitea"


def token_for_host(host_kind: str, *, fallback_env: str = "MAC_TASK_GIT_TOKEN") -> str:
    """Resolve the env-backed token for a known forge host.

    ``gitea`` -> ``GITEA_TOKEN``; ``github`` -> ``GH_TOKEN`` (or
    ``GITHUB_TOKEN``); anything else returns the ``fallback_env`` value
    if set. Returns an empty string when nothing is configured.
    """
    if host_kind == "github":
        return (
            os.environ.get("GH_TOKEN", "").strip()
            or os.environ.get("GITHUB_TOKEN", "").strip()
            or os.environ.get(fallback_env, "").strip()
        )
    if host_kind == "gitea":
        return os.environ.get("GITEA_TOKEN", "").strip() or os.environ.get(fallback_env, "").strip()
    return os.environ.get(fallback_env, "").strip()


def https_remote_for_token_auth(url: str) -> str:
    """Rewrite an SSH git remote to its https equivalent WHEN a token exists.

    Service processes (the hub's review publish/merge step in particular)
    cannot rely on interactive SSH state: keys and ssh-agent sockets are
    host-local incidentals that a redeploy or a wiped ``~/.ssh`` silently
    removes, after which every ``git@github.com:...`` clone fails with
    ``Permission denied (publickey)`` while a valid deploy token sits in the
    environment. When the host has an env-backed token, prefer the
    deterministic https form so :func:`inject_git_remote_auth` can carry the
    credential; without a token the URL passes through unchanged (SSH keys
    remain the auth story where they exist).

    Handles scp-like (``git@host:owner/repo.git``) and ``ssh://git@host/...``
    forms; everything else passes through untouched.
    """
    value = str(url or "").strip()
    host = ""
    owner_path = ""
    if value.startswith("ssh://"):
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        owner_path = parsed.path.lstrip("/")
    else:
        match = re.match(r"^[A-Za-z0-9._-]+@([A-Za-z0-9._-]+):(.+)$", value)
        if match:
            host = match.group(1).lower()
            owner_path = match.group(2).lstrip("/")
    if not host or not owner_path:
        return url
    try:
        host_kind = detect_host("https://%s" % host)
    except ValueError:
        return url
    if not token_for_host(host_kind):
        return url
    return "https://%s/%s" % (host, owner_path)


def inject_git_remote_auth(url: str) -> str:
    """Inject ``x-access-token:<pat>`` into a git remote URL.

    Single source of truth for token-rewriting — the K8s clone wrapper,
    host-mode worker fetch, the finalizer push, and the hub publish/merge
    all route through here.

    An SSH-form remote (``git@host:owner/repo`` or ``ssh://git@host/…``) is
    first normalized to its https equivalent WHEN an env-backed token exists
    for the host (see :func:`https_remote_for_token_auth`), so service
    processes that authenticate with a deploy token instead of interactive
    SSH keys — the fleet worker's canonical-branch fetch and the hub's
    publish, both of which failed with ``Permission denied (publickey)`` when
    ``~/.ssh`` was absent — carry the token consistently. Without a token the
    SSH URL is returned unchanged (keyed hosts keep working).
    """
    url = https_remote_for_token_auth(url)
    if not url or not url.startswith(("https://", "http://")):
        return url
    parts = urlsplit(url)
    if not parts.hostname:
        return url
    if parts.username:
        return url
    host_kind = detect_host(url)
    token = token_for_host(host_kind)
    if not token:
        return url
    user = "x-access-token"
    netloc = "%s:%s@%s" % (user, _quote(token, safe=""), parts.hostname)
    if parts.port:
        netloc = "%s:%d" % (netloc, parts.port)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def askpass_remote_auth(url: str) -> tuple[str, dict]:
    """A CLEAN url plus the environment that authenticates it.

    inject_git_remote_auth puts the credential in the URL, and the URL becomes
    argv the moment it reaches `git clone`. On the hub the whole token was
    readable from `ps` by any user on the box:

        git clone --no-tags --branch main -- https://x-access-token:<PAT>@github.com/...

    That is the exposure git_askpass.py was written to prevent -- its docstring
    says the credential "never enters argv, repository config, the task ledger,
    or a persistent credential store" -- but the hub publish and hub verify
    paths never used it.

    Returns the url untouched with an empty environment when no token applies,
    so SSH remotes and public clones behave exactly as before.
    """
    normalized = https_remote_for_token_auth(url)
    if not normalized or not normalized.startswith(("https://", "http://")):
        return url, {}
    parts = urlsplit(normalized)
    if not parts.hostname or parts.username:
        return normalized, {}
    token = token_for_host(detect_host(normalized))
    if not token:
        return normalized, {}
    askpass = Path(sys.executable).with_name("mac-git-askpass")
    if not askpass.is_file() or not os.access(askpass, os.X_OK):
        # Fall back rather than fail: a missing helper must not stop a
        # publication. The URL form still authenticates, and the caller's
        # existing redaction keeps it out of logs and evidence -- it is only
        # argv that stays exposed, which is the pre-existing behaviour.
        return inject_git_remote_auth(url), {}
    return normalized, {"GH_TOKEN": token, "GIT_ASKPASS": str(askpass)}


def strip_git_remote_auth(url: str) -> str:
    """Remove embedded HTTP credentials without leaving a redaction token.

    Evidence manifests intentionally redact push credentials.  A redacted URL
    is safe to display but is not a usable clone source: treating
    ``<redacted>`` as the password causes deterministic hub verification to
    reject valid work.  Strip all userinfo so the verifier can inject its own
    environment-backed credential, or clone a public repository anonymously.
    """

    if not url or not url.startswith(("https://", "http://")):
        return url
    parts = urlsplit(url)
    if not parts.hostname or not parts.username:
        return url
    netloc = parts.hostname
    if parts.port:
        netloc = "%s:%d" % (netloc, parts.port)
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def redact_git_remote_auth(url: str) -> str:
    """Return a display-safe git remote URL with embedded passwords hidden."""
    if not url or not url.startswith(("https://", "http://")):
        return url
    parts = urlsplit(url)
    if not parts.hostname or not parts.username:
        return url
    netloc = parts.hostname
    if parts.port:
        netloc = "%s:%d" % (netloc, parts.port)
    user = parts.username
    if parts.password is not None:
        netloc = "%s:<redacted>@%s" % (user, netloc)
    else:
        netloc = "<redacted>@%s" % netloc
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


_AUTHED_HTTP_REMOTE_RE = re.compile(r"(https?://)([^/\s:@]+):([^@\s/]+)@([^\s]+)")


def redact_git_remote_auth_in_text(value: str) -> str:
    """Redact embedded HTTPS git credentials in command output."""
    text = str(value or "")
    if "://" not in text or "@" not in text:
        return text
    return _AUTHED_HTTP_REMOTE_RE.sub(r"\1\2:<redacted>@\4", text)


def _git_timeout_scope(timeout: Optional[float]):
    class _Scope:
        def __enter__(self) -> None:
            self.prior = getattr(_GIT_TIMEOUT_STATE, "deadline", None)
            if timeout is None:
                self.deadline = self.prior
            else:
                self.deadline = time.monotonic() + max(0.001, float(timeout))
            _GIT_TIMEOUT_STATE.deadline = self.deadline

        def __exit__(self, *_args: object) -> None:
            _GIT_TIMEOUT_STATE.deadline = self.prior

    return _Scope()


def _remaining_git_timeout() -> Optional[float]:
    deadline = getattr(_GIT_TIMEOUT_STATE, "deadline", None)
    if deadline is None:
        return None
    return max(0.001, float(deadline) - time.monotonic())


def _git_timeout_scoped(function):
    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        timeout = kwargs.pop("timeout", None)
        with _git_timeout_scope(timeout):
            return function(*args, **kwargs)

    return wrapped


def _kill_git_process_tree(process: subprocess.Popen[str]) -> None:
    try:
        import psutil

        parent = psutil.Process(process.pid)
        descendants = parent.children(recursive=True)
        for item in reversed(descendants):
            try:
                item.kill()
            except psutil.NoSuchProcess:
                pass
        try:
            parent.kill()
        except psutil.NoSuchProcess:
            pass
    except Exception:  # noqa: BLE001 - retain the process-group fallback.
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, 9)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    try:
        if process.poll() is None:
            process.kill()
    except (ProcessLookupError, PermissionError, OSError):
        pass


def _run_git(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    argv = ["git", *args]
    timeout = _remaining_git_timeout()
    try:
        if timeout is None:
            return subprocess.run(
                argv,
                cwd=str(worktree),
                capture_output=True,
                text=True,
                check=False,
            )
        process = subprocess.Popen(
            argv,
            cwd=str(worktree),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            _kill_git_process_tree(process)
            stdout, stderr = process.communicate()
            return subprocess.CompletedProcess(
                argv,
                124,
                stdout or (exc.stdout if isinstance(exc.stdout, str) else ""),
                stderr
                or (exc.stderr if isinstance(exc.stderr, str) else "")
                or "git operation exceeded finalizer phase budget",
            )
        return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _git_failure(proc: subprocess.CompletedProcess[str], fallback: str) -> str:
    detail = (proc.stderr or proc.stdout or "").strip() or fallback
    return redact_git_remote_auth_in_text(detail)


@_git_timeout_scoped
def resolve_canonical_publication_target(
    *,
    worktree: Path,
    canonical_remote: str,
    canonical_branch: str,
    destination_branch: str,
    prepared_base_sha: str,
    isolation_key: str,
    timeout: Optional[float] = None,
) -> CanonicalPublicationTarget:
    """Resolve and validate the immutable target reused by check and push."""
    root = Path(worktree).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("publication worktree is missing: %s" % root)
    inside = _run_git(root, ["rev-parse", "--is-inside-work-tree"])
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        raise ValueError("publication worktree is not a git worktree: %s" % root)
    try:
        canonical_branch = validate_git_ref(str(canonical_branch or "").strip())
        destination_branch = validate_git_ref(str(destination_branch or "").strip())
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    for branch in (canonical_branch, destination_branch):
        checked = _run_git(root, ["check-ref-format", "--branch", branch])
        if checked.returncode != 0:
            raise ValueError("git branch failed check-ref-format: %r" % branch)

    remote_value = str(canonical_remote or "").strip()
    try:
        validate_git_remote_url(remote_value)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc

    prepared = str(prepared_base_sha or "").strip()
    if not prepared:
        raise ValueError("prepared repository base SHA is missing")
    if not _GIT_SHA_RE.fullmatch(prepared):
        raise ValueError("prepared repository base SHA is invalid: %r" % prepared)
    prepared_commit = _run_git(root, ["rev-parse", "--verify", "%s^{commit}" % prepared])
    if prepared_commit.returncode != 0 or prepared_commit.stdout.strip() != prepared:
        raise ValueError("prepared repository base is not a commit: %s" % prepared)

    head = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    task_head = head.stdout.strip()
    if head.returncode != 0 or not _GIT_SHA_RE.fullmatch(task_head):
        raise ValueError("could not resolve task HEAD: %s" % _git_failure(head, "invalid SHA"))
    prepared_ancestor = _run_git(root, ["merge-base", "--is-ancestor", prepared, task_head])
    if prepared_ancestor.returncode != 0:
        raise ValueError(
            "prepared base %s is not an ancestor of task HEAD %s" % (prepared[:12], task_head[:12])
        )

    key = str(isolation_key or "").strip()
    if not key:
        raise ValueError("publication isolation key is missing")
    isolated_ref = _isolated_publication_ref(key)
    ref_check = _run_git(root, ["check-ref-format", isolated_ref])
    if ref_check.returncode != 0:
        raise ValueError("isolated publication ref failed check-ref-format: %r" % isolated_ref)

    common, common_error = _git_common_directory(root)
    if common is None:
        raise ValueError(common_error)

    authed = inject_git_remote_auth(remote_value)
    return CanonicalPublicationTarget(
        worktree=root,
        canonical_remote_url=remote_value,
        remote=authed,
        remote_display=redact_git_remote_auth(authed),
        canonical_branch=canonical_branch,
        destination_branch=destination_branch,
        prepared_base_sha=prepared,
        task_head_sha=task_head,
        isolated_ref=isolated_ref,
        git_common_dir=common,
        lock_path=common / "mac_prepare_worktree.lock",
    )


def _git_common_directory(worktree: Path) -> tuple[Optional[Path], str]:
    result = _run_git(worktree, ["rev-parse", "--git-common-dir"])
    raw = result.stdout.strip()
    if result.returncode != 0 or not raw:
        return None, "could not resolve git common directory: %s" % _git_failure(
            result, "empty result"
        )
    common = Path(raw)
    if not common.is_absolute():
        common = (worktree / common).resolve()
    if not common.is_dir():
        return None, "git common directory is not a directory: %s" % common
    return common, ""


def _isolated_publication_ref(common_key: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(common_key or "task")).strip("-.")
    safe = (safe or "task")[:64]
    nonce = hashlib.sha256(
        ("%s:%s:%s" % (common_key, os.getpid(), time.time_ns())).encode("utf-8")
    ).hexdigest()[:16]
    return "refs/mac/publication/%s-%s" % (safe, nonce)


def _canonical_freshness_locked(
    target: CanonicalPublicationTarget,
    *,
    push: bool,
) -> CanonicalFreshnessResult:
    worktree = target.worktree
    head = _run_git(worktree, ["rev-parse", "--verify", "HEAD^{commit}"])
    head_sha = head.stdout.strip()
    if head.returncode != 0 or not _GIT_SHA_RE.fullmatch(head_sha):
        return CanonicalFreshnessResult(
            False,
            target,
            error="could not resolve task HEAD: %s" % _git_failure(head, "invalid SHA"),
        )
    if head_sha != target.task_head_sha:
        return CanonicalFreshnessResult(
            False,
            target,
            head_sha=head_sha,
            error="task HEAD changed after publication target resolution: %s -> %s"
            % (target.task_head_sha[:12], head_sha[:12]),
        )

    expected = _run_git(
        worktree, ["rev-parse", "--verify", "%s^{commit}" % target.prepared_base_sha]
    )
    if expected.returncode != 0:
        return CanonicalFreshnessResult(
            False,
            target,
            head_sha=head_sha,
            error="prepared repository base is not a commit: %s" % target.prepared_base_sha,
        )

    fetch_ref = target.isolated_ref
    fetch = _run_git(
        worktree,
        [
            "fetch",
            "--no-tags",
            target.remote,
            "+refs/heads/%s:%s" % (target.canonical_branch, fetch_ref),
        ],
    )
    result: Optional[CanonicalFreshnessResult] = None
    if fetch.returncode != 0:
        result = CanonicalFreshnessResult(
            False,
            target,
            head_sha=head_sha,
            error="fetch of canonical branch %r from %s failed: %s"
            % (
                target.canonical_branch,
                target.remote_display,
                _git_failure(fetch, "non-zero exit"),
            ),
        )
    else:
        resolve = _run_git(worktree, ["rev-parse", "--verify", "%s^{commit}" % fetch_ref])
        canonical_tip = resolve.stdout.strip()
        if resolve.returncode != 0 or not _GIT_SHA_RE.fullmatch(canonical_tip):
            result = CanonicalFreshnessResult(
                False,
                target,
                head_sha=head_sha,
                error="fetched canonical ref did not resolve to a commit: %s"
                % _git_failure(resolve, "invalid SHA"),
            )
        else:
            ancestor = _run_git(worktree, ["merge-base", "--is-ancestor", canonical_tip, head_sha])
            if ancestor.returncode != 0:
                result = CanonicalFreshnessResult(
                    False,
                    target,
                    head_sha=head_sha,
                    canonical_tip_sha=canonical_tip,
                    error=(
                        "canonical tip %s is not an ancestor of task HEAD %s; "
                        "rebase or merge %s before publication"
                    )
                    % (canonical_tip[:12], head_sha[:12], target.canonical_branch),
                )
            else:
                diff = _run_git(
                    worktree, ["diff", "--name-only", "%s..%s" % (canonical_tip, head_sha)]
                )
                if diff.returncode != 0:
                    result = CanonicalFreshnessResult(
                        False,
                        target,
                        head_sha=head_sha,
                        canonical_tip_sha=canonical_tip,
                        error="could not compute canonical diff: %s"
                        % _git_failure(diff, "non-zero exit"),
                    )
                else:
                    result = CanonicalFreshnessResult(
                        True,
                        target,
                        head_sha=head_sha,
                        canonical_tip_sha=canonical_tip,
                        files_changed=tuple(
                            line for line in diff.stdout.splitlines() if line.strip()
                        ),
                    )

    cleanup = _run_git(worktree, ["update-ref", "-d", fetch_ref])
    if cleanup.returncode != 0:
        return CanonicalFreshnessResult(
            False,
            target,
            head_sha=head_sha,
            canonical_tip_sha=(result.canonical_tip_sha if result else ""),
            files_changed=(result.files_changed if result else ()),
            error="could not clean isolated canonical fetch ref %s: %s"
            % (fetch_ref, _git_failure(cleanup, "non-zero exit")),
        )
    assert result is not None
    if not result.ok or not push:
        return result

    destination_ref = "refs/heads/%s" % target.destination_branch
    pushed = _run_git(worktree, ["push", target.remote, "HEAD:%s" % destination_ref])
    if pushed.returncode != 0:
        return CanonicalFreshnessResult(
            False,
            target,
            head_sha=result.head_sha,
            canonical_tip_sha=result.canonical_tip_sha,
            files_changed=result.files_changed,
            error="git push to %s failed: %s"
            % (target.remote_display, _git_failure(pushed, "non-zero exit")),
            push_returncode=int(pushed.returncode),
            push_stdout=redact_git_remote_auth_in_text(pushed.stdout or ""),
            push_stderr=redact_git_remote_auth_in_text(pushed.stderr or ""),
        )
    remote = _run_git(worktree, ["ls-remote", target.remote, destination_ref])
    remote_sha = (remote.stdout.split() or [""])[0] if remote.returncode == 0 else ""
    if remote_sha != result.head_sha:
        return CanonicalFreshnessResult(
            False,
            target,
            head_sha=result.head_sha,
            canonical_tip_sha=result.canonical_tip_sha,
            files_changed=result.files_changed,
            error="push completed but remote branch verification failed for %s" % destination_ref,
            push_returncode=0,
            push_stdout=redact_git_remote_auth_in_text(pushed.stdout or ""),
            push_stderr=redact_git_remote_auth_in_text(pushed.stderr or ""),
        )
    return CanonicalFreshnessResult(
        True,
        target,
        head_sha=result.head_sha,
        canonical_tip_sha=result.canonical_tip_sha,
        files_changed=result.files_changed,
        push_returncode=0,
        push_stdout=redact_git_remote_auth_in_text(pushed.stdout or ""),
        push_stderr=redact_git_remote_auth_in_text(pushed.stderr or ""),
        remote_verified=True,
    )


@_git_timeout_scoped
def sync_worktree_with_canonical(
    worktree: Path,
    canonical_remote: str,
    canonical_branch: str,
    *,
    timeout: Optional[float] = None,
) -> Dict[str, str]:
    """Rebase the task worktree onto the advanced canonical tip BEFORE the
    contract test runs, so the suite validates the projected published tree.

    Fleet agents race each other to one canonical branch; without this, every
    task slower than its peers dies at the publication freshness gate
    ("canonical tip … is not an ancestor of task HEAD") after an hour of good
    work. A CLEAN rebase only: conflicts abort (`git rebase --abort`), the work
    stays intact, and the existing freshness gate then reports its precise
    error for a human/agent to resolve. Callers must have committed all local
    changes first (both finalizers auto-commit before verification) and must
    re-read HEAD when status == "rebased".
    """
    remote = str(canonical_remote or "").strip()
    branch = str(canonical_branch or "").strip() or "main"
    if not remote:
        return {"status": "skipped", "reason": "no canonical remote"}
    authed = inject_git_remote_auth(remote)
    fetch = _run_git(worktree, ["fetch", "--no-tags", authed, branch])
    if fetch.returncode != 0:
        return {
            "status": "fetch_failed",
            "reason": redact_git_remote_auth_in_text(_git_failure(fetch, "non-zero exit"))[:500],
        }
    tip_res = _run_git(worktree, ["rev-parse", "--verify", "FETCH_HEAD^{commit}"])
    tip = tip_res.stdout.strip()
    if tip_res.returncode != 0 or not _GIT_SHA_RE.fullmatch(tip):
        return {"status": "fetch_failed", "reason": "FETCH_HEAD did not resolve to a commit"}
    if _run_git(worktree, ["merge-base", "--is-ancestor", tip, "HEAD"]).returncode == 0:
        return {"status": "fresh", "canonical_tip": tip}
    rebase = _run_git(
        worktree,
        ["-c", "user.email=mac-fleet@nvidia.com", "-c", "user.name=MAC fleet", "rebase", tip],
    )
    if rebase.returncode != 0:
        _run_git(worktree, ["rebase", "--abort"])
        return {
            "status": "conflict",
            "canonical_tip": tip,
            "reason": redact_git_remote_auth_in_text(
                ((rebase.stderr or rebase.stdout) or "rebase failed").strip()
            )[:500],
        }
    return {"status": "rebased", "canonical_tip": tip}


@_git_timeout_scoped
def check_canonical_freshness(
    target: CanonicalPublicationTarget,
    *,
    timeout: Optional[float] = None,
) -> CanonicalFreshnessResult:
    """Fetch and verify canonical ancestry without publishing anything."""
    return _canonical_publication_operation(target, push=False)


@_git_timeout_scoped
def guarded_push(
    target: CanonicalPublicationTarget,
    *,
    timeout: Optional[float] = None,
) -> CanonicalFreshnessResult:
    """Re-fetch canonical state and push only when task HEAD contains it.

    Validation, canonical fetch, ancestry checking, temporary-ref cleanup,
    publication, and remote verification all happen under the repository's
    shared git-common-dir lock. The authenticated URL checked here is the exact
    URL passed to both ``git push`` and ``git ls-remote``.
    """
    return _canonical_publication_operation(target, push=True)


def _canonical_publication_operation(
    target: CanonicalPublicationTarget,
    *,
    push: bool,
) -> CanonicalFreshnessResult:
    try:
        lock = target.lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        return CanonicalFreshnessResult(
            False,
            target,
            head_sha=target.task_head_sha,
            error="could not open publication lock: %s" % exc,
        )
    result: Optional[CanonicalFreshnessResult] = None
    try:
        try:
            remaining = _remaining_git_timeout()
            if remaining is None:
                fcntl.flock(lock, fcntl.LOCK_EX)
            else:
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        remaining = _remaining_git_timeout()
                        if remaining is not None and remaining <= 0.01:
                            return CanonicalFreshnessResult(
                                False,
                                target,
                                head_sha=target.task_head_sha,
                                error="timed out acquiring canonical publication lock",
                            )
                        time.sleep(min(0.05, remaining) if remaining is not None else 0.05)
        except OSError as exc:
            return CanonicalFreshnessResult(
                False,
                target,
                head_sha=target.task_head_sha,
                error="could not acquire publication lock: %s" % exc,
            )
        result = _canonical_freshness_locked(target, push=push)
        try:
            fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError as exc:
            return CanonicalFreshnessResult(
                False,
                target,
                head_sha=result.head_sha,
                canonical_tip_sha=result.canonical_tip_sha,
                files_changed=result.files_changed,
                error="could not release publication lock: %s" % exc,
            )
        return result
    finally:
        lock.close()


def _parse_owner_repo(repo_url: str) -> Tuple[str, str]:
    parsed = urlparse(repo_url if "://" in repo_url else "https://" + repo_url)
    path = (parsed.path or "").strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        raise ValueError("repo URL missing owner/repo: %s" % repo_url)
    return parts[0], parts[1]


def _api_base_for(host_kind: str, repo_url: str) -> str:
    parsed = urlparse(repo_url if "://" in repo_url else "https://" + repo_url)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or ""
    port = (":" + str(parsed.port)) if parsed.port else ""
    if host_kind == "github":
        return "https://api.github.com"
    return "%s://%s%s/api/v1" % (scheme, host, port)


def _http_post_json(url: str, headers: dict, body: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return json.loads(data) if data else {}
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            "POST %s -> %d %s: %s" % (url, exc.code, exc.reason, body_text[:500])
        ) from exc


def _http_get_json(url: str, headers: dict, timeout: float = 20.0) -> dict:
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read().decode("utf-8")
        return json.loads(data) if data else {}


def open_pull_request(
    repo_url: str,
    head: str,
    *,
    base: Optional[str] = None,
    title: Optional[str] = None,
    body: Optional[str] = None,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> PullRequestResult:
    """Open a PR/MR on github.com or a self-hosted gitea.

    ``head`` is the branch name (not ``owner:branch``). ``base`` defaults
    to the upstream's default branch when omitted. Raises ``ValueError``
    if the required token is missing.
    """
    host_kind = detect_host(repo_url)
    owner, repo = _parse_owner_repo(repo_url)
    api_base = _api_base_for(host_kind, repo_url)

    if host_kind == "github":
        token = github_token or token_for_host("github")
        if not token:
            raise ValueError("GH_TOKEN required to open a github PR")
        headers = {
            "Authorization": "token " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "mac-gitops",
        }
    else:
        token = gitea_token or token_for_host("gitea")
        if not token:
            raise ValueError("GITEA_TOKEN required to open a gitea PR")
        headers = {
            "Authorization": "token " + token,
            "User-Agent": "mac-gitops",
        }

    if not base:
        repo_meta = _http_get_json("%s/repos/%s/%s" % (api_base, owner, repo), headers)
        base = str(repo_meta.get("default_branch") or "main")

    title = title or ("mac: %s" % head)
    body = body or ""

    create_url = "%s/repos/%s/%s/pulls" % (api_base, owner, repo)
    payload = {"title": title, "body": body, "head": head, "base": base}

    try:
        pr = _http_post_json(create_url, headers, payload)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if (
            "already exists" in msg
            or "pull request already exists" in msg
            or "exists for these targets" in msg
        ):
            existing = _find_existing_pr(api_base, headers, owner, repo, host_kind, head, base)
            if existing is not None:
                return existing
        raise

    return PullRequestResult(
        host=host_kind,
        number=int(pr.get("number") or 0),
        url=str(pr.get("html_url") or pr.get("url") or ""),
        state=str(pr.get("state") or "open"),
    )


def _find_existing_pr(
    api_base: str,
    headers: dict,
    owner: str,
    repo: str,
    host_kind: str,
    head: str,
    base: str,
) -> Optional[PullRequestResult]:
    if host_kind == "github":
        list_url = "%s/repos/%s/%s/pulls?head=%s:%s&base=%s&state=open" % (
            api_base,
            owner,
            repo,
            owner,
            head,
            base,
        )
    else:
        list_url = "%s/repos/%s/%s/pulls?state=open" % (api_base, owner, repo)
    try:
        with urllib.request.urlopen(
            urllib.request.Request(list_url, headers=headers, method="GET"),
            timeout=20.0,
        ) as resp:
            data = json.loads(resp.read().decode("utf-8") or "[]")
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    for pr in data:
        if not isinstance(pr, dict):
            continue
        head_ref = (pr.get("head") or {}).get("ref") if isinstance(pr.get("head"), dict) else None
        if head_ref == head:
            return PullRequestResult(
                host=host_kind,
                number=int(pr.get("number") or 0),
                url=str(pr.get("html_url") or pr.get("url") or ""),
                state=str(pr.get("state") or "open"),
            )
    return None


def _scrub_secret(text: str, *secrets: Optional[str]) -> str:
    """Remove credential material from text that may reach a log or a ledger.

    Forge API errors are echoed into publication evidence and operator-facing
    diagnoses.  A token never appears in a request URL here (it travels in the
    ``Authorization`` header), but a misconfigured proxy, a redirect, or a
    forge that reflects a header back into its error body would leak it, so
    every string that escapes this module is scrubbed unconditionally.
    """
    scrubbed = redact_git_remote_auth_in_text(str(text or ""))
    for secret in secrets:
        value = str(secret or "").strip()
        if len(value) >= 8:
            scrubbed = scrubbed.replace(value, "***")
    return scrubbed


def _forge_api_context(
    repo_url: str,
    *,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> Tuple[str, str, str, str, Dict[str, str], str]:
    """Resolve (host_kind, owner, repo, api_base, headers, token) for a forge."""
    host_kind = detect_host(repo_url)
    owner, repo = _parse_owner_repo(repo_url)
    api_base = _api_base_for(host_kind, repo_url)
    if host_kind == "github":
        token = github_token or token_for_host("github")
        if not token:
            raise ValueError("GH_TOKEN required to call the github API")
        headers = {
            "Authorization": "token " + token,
            "Accept": "application/vnd.github+json",
            "User-Agent": "mac-gitops",
        }
    else:
        token = gitea_token or token_for_host("gitea")
        if not token:
            raise ValueError("GITEA_TOKEN required to call the gitea API")
        headers = {"Authorization": "token " + token, "User-Agent": "mac-gitops"}
    return host_kind, owner, repo, api_base, headers, token


def resolve_forge(repo_url: str) -> Optional[str]:
    """Return the forge kind when ``repo_url`` is an API-reachable forge.

    ``None`` means "this remote has no forge we can open a pull request on" —
    a ``file://`` or bare-path remote, an ``ssh://`` remote with no token to
    rewrite it, or an http(s) remote for which no credential is configured.
    Publication uses this to decide whether the pull-request strategy is even
    possible; a ``None`` here is what makes the direct-push fallback legitimate
    rather than a silent downgrade of a repo that could have used a PR.
    """
    value = https_remote_for_token_auth(str(repo_url or "").strip())
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        host_kind = detect_host(value)
        _parse_owner_repo(value)
    except ValueError:
        return None
    return host_kind if token_for_host(host_kind) else None


def required_status_check_contexts(
    repo_url: str,
    branch: str,
    *,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> Optional[Tuple[str, ...]]:
    """Status checks the forge requires before ``branch`` may be merged into.

    Returns ``None`` when the answer is unknown (unsupported forge, API error,
    insufficient scope).  Callers must treat ``None`` and ``()`` as "the forge
    is not gating this merge for us" and keep their own gate.

    Uses GitHub's ``/rules/branches/{branch}`` endpoint rather than the branch
    protection API because it needs no admin scope and reports rulesets, which
    is how this repository's ``main`` is actually protected.
    """
    try:
        host_kind, owner, repo, api_base, headers, token = _forge_api_context(
            repo_url, github_token=github_token, gitea_token=gitea_token
        )
    except ValueError:
        return None
    if host_kind != "github":
        return None
    url = "%s/repos/%s/%s/rules/branches/%s" % (
        api_base,
        owner,
        repo,
        _quote(str(branch or ""), safe=""),
    )
    try:
        rules = _http_get_json(url, headers)
    except Exception:  # noqa: BLE001 - an unknown answer must not block publication
        return None
    if not isinstance(rules, list):
        return None
    contexts: list[str] = []
    for rule in rules:
        if not isinstance(rule, dict) or rule.get("type") != "required_status_checks":
            continue
        params = rule.get("parameters")
        checks = params.get("required_status_checks") if isinstance(params, dict) else None
        for check in checks or []:
            if isinstance(check, dict) and str(check.get("context") or "").strip():
                contexts.append(str(check["context"]).strip())
    return tuple(dict.fromkeys(contexts))


def _http_put_json(
    url: str, headers: dict, body: dict, timeout: float = 30.0
) -> Tuple[int, dict, str]:
    """PUT JSON, returning (status, decoded_body, error_text) without raising.

    The merge endpoint answers "not yet" with a 4xx that is *expected*, so the
    status code is data here rather than an exception.
    """
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="PUT",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            decoded = json.loads(raw) if raw else {}
            return int(resp.status or 0), (decoded if isinstance(decoded, dict) else {}), ""
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw) if raw else {}
        except ValueError:
            decoded = {}
        message = ""
        if isinstance(decoded, dict):
            message = str(decoded.get("message") or "")
        return (
            int(exc.code),
            (decoded if isinstance(decoded, dict) else {}),
            (message or raw[:500] or str(exc.reason)),
        )
    except urllib.error.URLError as exc:
        return 0, {}, str(exc.reason)


# Forge responses that mean "the PR is fine, its gates have not passed yet".
_MERGE_BLOCKED_MARKERS = (
    "required status check",
    "required status checks",
    "checks have not",
    "not mergeable",
    "is not mergeable",
    "review is required",
    "changes requested",
    "protected branch",
    "branch protection",
    "merge conflict",
    "waiting on code owner",
    "expected head sha",
    "head sha",
)


def merge_pull_request(
    repo_url: str,
    number: int,
    *,
    method: str = "squash",
    sha: Optional[str] = None,
    commit_title: Optional[str] = None,
    commit_message: Optional[str] = None,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> PullRequestMergeResult:
    """Ask the forge to merge PR ``number``; squash by default.

    ``sha`` pins the head the caller reviewed: the forge refuses the merge if
    the branch moved underneath us, which is the pull-request equivalent of the
    direct-push path's ``--force-with-lease``.

    A refusal that names the forge's own gates comes back as
    ``blocked=True`` so the caller can retry cheaply once the checks finish,
    instead of turning a normal "CI is still running" into a publication
    failure.
    """
    if int(number) <= 0:
        raise ValueError("pull request number is required to merge")
    if method not in {"squash", "merge", "rebase"}:
        raise ValueError("unsupported merge method: %r" % method)
    host_kind, owner, repo, api_base, headers, token = _forge_api_context(
        repo_url, github_token=github_token, gitea_token=gitea_token
    )

    url = "%s/repos/%s/%s/pulls/%d/merge" % (api_base, owner, repo, int(number))
    if host_kind == "github":
        body: Dict[str, object] = {"merge_method": method}
        if commit_title:
            body["commit_title"] = commit_title
        if commit_message:
            body["commit_message"] = commit_message
        if sha:
            body["sha"] = sha
    else:
        body = {"Do": method}
        if commit_title:
            body["MergeTitleField"] = commit_title
        if commit_message:
            body["MergeMessageField"] = commit_message
        if sha:
            body["head_commit_id"] = sha

    status, decoded, error = _http_put_json(url, headers, body)
    if 200 <= status < 300:
        merged_sha = str(decoded.get("sha") or "").strip()
        if not merged_sha and host_kind != "github":
            merged_sha = _merged_sha_for(api_base, headers, owner, repo, int(number))
        return PullRequestMergeResult(merged=True, number=int(number), sha=merged_sha)

    reason = _scrub_secret(error or ("HTTP %d" % status), token)
    lowered = reason.lower()
    blocked = status in {405, 409, 422} and any(
        marker in lowered for marker in _MERGE_BLOCKED_MARKERS
    )
    if blocked:
        return PullRequestMergeResult(merged=False, number=int(number), blocked=True, reason=reason)
    raise RuntimeError(
        "merge of pull request #%d failed (HTTP %d): %s" % (int(number), status, reason)
    )


def _merged_sha_for(api_base: str, headers: dict, owner: str, repo: str, number: int) -> str:
    try:
        pr = _http_get_json("%s/repos/%s/%s/pulls/%d" % (api_base, owner, repo, number), headers)
    except Exception:  # noqa: BLE001 - the merge already succeeded
        return ""
    if not isinstance(pr, dict):
        return ""
    return str(pr.get("merge_commit_sha") or pr.get("merged_commit_id") or "").strip()


# ----------------------------------------------------------------------
# Verify, do not assume: the requester checks that the gates actually ran.
# ----------------------------------------------------------------------

# A required context that reported one of these has genuinely failed; retrying
# will not change it.
_CHECK_FAILED_CONCLUSIONS = {
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "stale",
    "startup_failure",
}
# Everything else that is not "success" -- queued, in_progress, neutral,
# SKIPPED, or no report at all -- means the gate has not passed. Skipped is
# deliberately NOT success: a required check that did not run is exactly the
# "green gate enforcing nothing" shape this verification exists to catch.


def required_check_verdicts(
    repo_url: str,
    sha: str,
    contexts: Tuple[str, ...],
    *,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> Dict[str, object]:
    """Did each required context actually pass for ``sha``?

    DO NOT RELY ON THE FORGE REFUSING. An identity that holds a ruleset bypass
    -- the repository owner, which is what the fleet authenticates as -- can
    merge straight past required status checks, and did: the first publication
    under the pull-request flow merged two seconds after the PR was created,
    with every required context reported SKIPPED. Nothing gated it: not the
    forge (bypassed) and not the hub (which skips its own contract
    re-projection precisely *because* the forge reports required checks).

    So the party requesting the merge verifies first, instead of assuming a
    refusal will arrive if the checks have not passed. This holds whether or
    not the identity has a bypass, and it composes with a merge queue rather
    than duplicating it: the queue serializes and tests the candidate, this
    makes sure nobody asks for a merge that was never validated.

    Returns ``passed``/``pending``/``failed`` lists plus ``known``.  ``known``
    is False when the forge could not be asked, which callers must treat as
    "not verified" -- never as "fine".
    """
    verdict: Dict[str, object] = {
        "known": False,
        "contexts": list(contexts),
        "passed": [],
        "pending": list(contexts),
        "failed": [],
    }
    if not contexts:
        verdict["known"] = True
        verdict["pending"] = []
        return verdict
    try:
        host_kind, owner, repo, api_base, headers, _token = _forge_api_context(
            repo_url, github_token=github_token, gitea_token=gitea_token
        )
    except ValueError:
        return verdict
    if host_kind != "github":
        return verdict

    latest: Dict[str, str] = {}
    try:
        combined = _http_get_json(
            "%s/repos/%s/%s/commits/%s/status" % (api_base, owner, repo, _quote(sha, safe="")),
            headers,
        )
    except Exception:  # noqa: BLE001 - an unknown answer is "not verified"
        return verdict
    for status in (combined or {}).get("statuses") or []:
        if isinstance(status, dict) and str(status.get("context") or ""):
            latest.setdefault(str(status["context"]), str(status.get("state") or ""))
    try:
        runs = _http_get_json(
            "%s/repos/%s/%s/commits/%s/check-runs?per_page=100"
            % (api_base, owner, repo, _quote(sha, safe="")),
            headers,
        )
    except Exception:  # noqa: BLE001
        runs = {}
    for run in (runs or {}).get("check_runs") or []:
        if not isinstance(run, dict):
            continue
        name = str(run.get("name") or "")
        if not name:
            continue
        if str(run.get("status") or "") != "completed":
            latest[name] = "pending"
            continue
        latest[name] = str(run.get("conclusion") or "")

    passed: list[str] = []
    pending: list[str] = []
    failed: list[str] = []
    for context in contexts:
        outcome = latest.get(context, "")
        if outcome == "success":
            passed.append(context)
        elif outcome in _CHECK_FAILED_CONCLUSIONS or outcome == "failure":
            failed.append(context)
        else:
            pending.append(context)
    verdict.update({"known": True, "passed": passed, "pending": pending, "failed": failed})
    return verdict


# ----------------------------------------------------------------------
# Merge queue: serialize the merges without serializing the test runs.
# ----------------------------------------------------------------------


def merge_queue_enabled(
    repo_url: str,
    branch: str,
    *,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> Optional[bool]:
    """Whether ``branch`` is landed through the forge's merge queue.

    ``True``/``False`` are answers; ``None`` means "unknown" (a forge with no
    merge queue at all, an API error, or insufficient scope).  Callers must
    treat ``None`` exactly like ``False`` *and say so in their evidence*: a
    repository without a queue gets a plain squash merge, which does not carry
    the queue's guarantee, and silently assuming otherwise just relocates the
    hole.

    Reuses the same ``/rules/branches/{branch}`` endpoint as
    :func:`required_status_check_contexts` — no admin scope, and it reports
    rulesets, which is how protection is actually configured here.
    """
    try:
        host_kind, owner, repo, api_base, headers, _token = _forge_api_context(
            repo_url, github_token=github_token, gitea_token=gitea_token
        )
    except ValueError:
        return None
    if host_kind != "github":
        # gitea has no merge-queue equivalent. "Unknown" rather than False so
        # the caller records "this forge cannot serialize merges for us".
        return None
    url = "%s/repos/%s/%s/rules/branches/%s" % (
        api_base,
        owner,
        repo,
        _quote(str(branch or ""), safe=""),
    )
    try:
        rules = _http_get_json(url, headers)
    except Exception:  # noqa: BLE001 - an unknown answer must not block publication
        return None
    if not isinstance(rules, list):
        return None
    for rule in rules:
        if isinstance(rule, dict) and rule.get("type") == "merge_queue":
            return True
    return False


def _graphql_url(host_kind: str, repo_url: str) -> str:
    parsed = urlparse(repo_url if "://" in repo_url else "https://" + repo_url)
    if host_kind == "github" and (parsed.hostname or "").lower() in {
        "github.com",
        "api.github.com",
        "www.github.com",
    }:
        return "https://api.github.com/graphql"
    scheme = parsed.scheme or "https"
    port = (":" + str(parsed.port)) if parsed.port else ""
    return "%s://%s%s/api/graphql" % (scheme, parsed.hostname or "", port)


def _graphql(url: str, headers: dict, query: str, variables: dict, token: str) -> Tuple[dict, str]:
    """POST a GraphQL document; return ``(data, error_text)`` without raising.

    GitHub answers a rejected mutation with HTTP 200 and an ``errors`` array,
    so errors are data here in exactly the way the merge endpoint's 4xx is.
    """
    body = {"query": query, "variables": variables}
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30.0) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        return {}, _scrub_secret("HTTP %d %s: %s" % (exc.code, exc.reason, raw[:500]), token)
    except urllib.error.URLError as exc:
        return {}, _scrub_secret(str(exc.reason), token)
    try:
        decoded = json.loads(raw) if raw else {}
    except ValueError:
        return {}, _scrub_secret("unparseable GraphQL response", token)
    if not isinstance(decoded, dict):
        return {}, _scrub_secret("unexpected GraphQL response", token)
    errors = decoded.get("errors")
    if errors:
        messages = [str(err.get("message") or "") for err in errors if isinstance(err, dict)]
        return _ensure_mapping(decoded.get("data")), _scrub_secret(
            "; ".join(m for m in messages if m)[:500] or "GraphQL error", token
        )
    return _ensure_mapping(decoded.get("data")), ""


def _ensure_mapping(value: object) -> dict:
    """Return ``value`` when it is a dict, otherwise an empty dict."""
    return value if isinstance(value, dict) else {}


_PULL_REQUEST_NODE_QUERY = """
query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){
    pullRequest(number:$number){
      id state merged headRefOid
      mergeCommit{ oid }
    }
  }
}
"""

_ENQUEUE_MUTATION = """
mutation($pullRequestId:ID!,$expectedHeadOid:GitObjectID!){
  enqueuePullRequest(input:{pullRequestId:$pullRequestId,expectedHeadOid:$expectedHeadOid}){
    mergeQueueEntry{ id position state }
  }
}
"""

# GraphQL refusals that mean "the PR is fine, it is simply not landable yet".
_ENQUEUE_BLOCKED_MARKERS = (
    "not mergeable",
    "is not in a mergeable state",
    "required status check",
    "checks have not",
    "review is required",
    "changes requested",
    "already queued",
    "already in the merge queue",
    "pull request is in an unstable",
    "merge queue is not enabled",
    "base branch modified",
    "head sha",
    "expected head",
    "waiting on code owner",
    "protected branch",
)


def pull_request_state(
    repo_url: str,
    number: int,
    *,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> Dict[str, object]:
    """Current forge state of PR ``number``: merged, its SHA, and its head.

    Publication is retried, and between attempts a queued PR may have landed
    on its own.  Asking the forge first is what makes "the merge queue merged
    it while we were backing off" a success rather than a second merge
    attempt.
    """
    host_kind, owner, repo, api_base, headers, token = _forge_api_context(
        repo_url, github_token=github_token, gitea_token=gitea_token
    )
    try:
        pr = _http_get_json(
            "%s/repos/%s/%s/pulls/%d" % (api_base, owner, repo, int(number)), headers
        )
    except Exception as exc:  # noqa: BLE001 - unknown state is not fatal
        return {
            "known": False,
            "merged": False,
            "sha": "",
            "state": "",
            "head_sha": "",
            "error": _scrub_secret(str(exc), token)[:300],
        }
    if not isinstance(pr, dict):
        return {"known": False, "merged": False, "sha": "", "state": "", "head_sha": ""}
    head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
    return {
        "known": True,
        "merged": bool(pr.get("merged") or pr.get("merged_at")),
        "sha": str(pr.get("merge_commit_sha") or pr.get("merged_commit_id") or "").strip(),
        "state": str(pr.get("state") or ""),
        "head_sha": str((head or {}).get("sha") or "").strip(),
        "host": host_kind,
    }


def enqueue_pull_request(
    repo_url: str,
    number: int,
    *,
    sha: str,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> PullRequestMergeResult:
    """Add PR ``number`` to the forge's merge queue, pinned to ``sha``.

    ``sha`` is the reviewed head, passed as ``expectedHeadOid``: the forge
    refuses the enqueue if the branch moved underneath us.  That is the same
    safety property the direct merge gets from its ``sha`` parameter and the
    direct-push path gets from ``--force-with-lease``.

    The queue lands the PR asynchronously, so a *successful* enqueue returns
    ``merged=False, queued=True``: the caller defers through its existing
    retry backoff and observes the merge on a later attempt.  A refusal that
    names the forge's own gates comes back ``blocked=True``; anything else
    raises.
    """
    if int(number) <= 0:
        raise ValueError("pull request number is required to enqueue")
    if not str(sha or "").strip():
        raise ValueError("a reviewed head sha is required to enqueue")
    host_kind, owner, repo, _api_base, headers, token = _forge_api_context(
        repo_url, github_token=github_token, gitea_token=gitea_token
    )
    if host_kind != "github":
        raise ValueError("merge queue enqueue is only supported on github")
    url = _graphql_url(host_kind, repo_url)
    data, error = _graphql(
        url,
        headers,
        _PULL_REQUEST_NODE_QUERY,
        {"owner": owner, "name": repo, "number": int(number)},
        token,
    )
    pull = _ensure_mapping(
        _ensure_mapping(_ensure_mapping(data).get("repository")).get("pullRequest")
    )
    if error or not pull.get("id"):
        raise RuntimeError(
            "could not resolve pull request #%d for the merge queue: %s"
            % (int(number), error or "no pull request node")
        )
    if bool(pull.get("merged")):
        return PullRequestMergeResult(
            merged=True,
            number=int(number),
            sha=str(_ensure_mapping(pull.get("mergeCommit")).get("oid") or "").strip(),
            serialization="merge_queue",
        )

    _data, error = _graphql(
        url,
        headers,
        _ENQUEUE_MUTATION,
        {"pullRequestId": str(pull["id"]), "expectedHeadOid": str(sha).strip()},
        token,
    )
    if not error:
        return PullRequestMergeResult(
            merged=False,
            number=int(number),
            queued=True,
            serialization="merge_queue",
            reason="enqueued into the merge queue",
        )
    lowered = error.lower()
    if any(marker in lowered for marker in _ENQUEUE_BLOCKED_MARKERS):
        return PullRequestMergeResult(
            merged=False,
            number=int(number),
            blocked=True,
            serialization="merge_queue",
            reason=error,
        )
    raise RuntimeError("enqueue of pull request #%d failed: %s" % (int(number), error))


def request_pull_request_merge(
    repo_url: str,
    number: int,
    *,
    sha: str,
    branch: str,
    method: str = "squash",
    commit_title: Optional[str] = None,
    commit_message: Optional[str] = None,
    queue_enabled: Optional[bool] = None,
    github_token: Optional[str] = None,
    gitea_token: Optional[str] = None,
) -> PullRequestMergeResult:
    """Ask the forge to land PR ``number``, through its merge queue if there is one.

    THE GUARANTEE, written down rather than assumed:

    * **With a merge queue** (``queue_enabled`` True): the queue builds a
      speculative merge candidate, runs the required checks against the
      *projected post-merge* tree, and merges in order.  What was tested is
      what lands — the property :mod:`mac.merge_queue` relies on, restored
      without the serial-rebase cost of ``strict`` required status checks
      (the queue serializes the merges, not the test runs).
    * **Without one** (``None`` or False — a repo with no queue configured,
      gitea, or an unreadable ruleset): a plain squash merge.  Required
      checks alone do NOT guarantee the landed tree was tested, because the
      canonical branch can advance between the checks finishing and the merge
      executing.  The caller must re-validate the canonical tip immediately
      before calling this, and the returned ``serialization`` says
      ``direct_squash`` so the weaker guarantee is visible in the evidence.

    Already-merged PRs (the queue landed it between attempts) return
    ``merged=True`` rather than being merged twice.
    """
    if queue_enabled:
        observed = pull_request_state(
            repo_url, number, github_token=github_token, gitea_token=gitea_token
        )
        if observed.get("known") and observed.get("merged"):
            return PullRequestMergeResult(
                merged=True,
                number=int(number),
                sha=str(observed.get("sha") or ""),
                serialization="merge_queue",
            )
        return enqueue_pull_request(
            repo_url,
            number,
            sha=sha,
            github_token=github_token,
            gitea_token=gitea_token,
        )
    result = merge_pull_request(
        repo_url,
        number,
        method=method,
        sha=sha,
        commit_title=commit_title,
        commit_message=commit_message,
        github_token=github_token,
        gitea_token=gitea_token,
    )
    result.serialization = "direct_squash"
    return result


def open_pull_request_for_target(
    target: "CanonicalPublicationTarget",
    *,
    title: str,
    body: str,
) -> Dict[str, object]:
    """Open the task's pull request FROM THE AGENT, onto the canonical branch.

    This runs in the worker/finalizer process, right after ``guarded_push``
    placed the task branch on the remote — the agent publishes its own work
    rather than asking the hub to do it.  The base is
    ``target.canonical_branch``, which comes from the task's repository
    contract; it is not assumed to be ``main``.

    Never raises: a repository with no API-reachable forge (``file://``, a
    bare path, no token) is a legitimate configuration, and a forge hiccup
    must not fail a finalizer whose work is already pushed.  The outcome —
    including *why* no PR exists — is returned for the evidence manifest.
    """
    remote_url = str(getattr(target, "canonical_remote_url", "") or "").strip()
    base = str(getattr(target, "canonical_branch", "") or "").strip()
    head = str(getattr(target, "destination_branch", "") or "").strip()
    if not (remote_url and base and head):
        return {
            "opened": False,
            "reason": "publication target has no canonical remote/branch",
        }
    if head == base:
        return {
            "opened": False,
            "reason": "task publishes directly to the canonical branch; no pull request",
        }
    forge = resolve_forge(remote_url)
    token = ""
    if not forge:
        # No env-backed token is not the end of the question: the hub is the
        # fleet's credential store, so ask it by name before giving up.
        try:
            host_kind = detect_host(remote_url)
        except ValueError:
            host_kind = ""
        if host_kind and str(remote_url).startswith(("http://", "https://")):
            try:
                token = forge_token_from_hub(host_kind)
            except ValueError as exc:
                return {"opened": False, "reason": str(exc)}
            if token:
                forge = host_kind
    if not forge:
        return {
            "opened": False,
            "reason": (
                "canonical remote has no API-reachable forge (no http(s) forge "
                "URL, and no credential from the environment or the hub secret "
                "store for its host)"
            ),
        }
    api_url = https_remote_for_token_auth(remote_url)
    try:
        pr = open_pull_request(
            api_url,
            head,
            base=base,
            title=title,
            body=body,
            **({"github_token": token} if token and forge == "github" else {}),
            **({"gitea_token": token} if token and forge == "gitea" else {}),
        )
    except Exception as exc:  # noqa: BLE001 - reported, never fatal
        # The token never reaches the evidence manifest, which is read by
        # operators and stored in the ledger.
        return {
            "opened": False,
            "forge": forge,
            "base": base,
            "head": head,
            "reason": _scrub_secret(str(exc), token or token_for_host(forge))[:400],
        }
    return {
        "opened": True,
        "forge": forge,
        "number": int(pr.number),
        "url": str(pr.url),
        "state": str(pr.state),
        "base": base,
        "head": head,
        "opened_by": "agent",
    }


# The hub is the fleet's credential store. An agent that has no forge token in
# its own environment asks the hub for one BY NAME, at the moment of use.
_FORGE_SECRET_NAMES = {"github": "github.token", "gitea": "gitea.token"}


def forge_token_from_hub(host_kind: str) -> str:
    """Resolve the forge credential from the hub's audited secret store.

    Resolution is BY NAME (``github.token``), never by id, so a rotation does
    not break publication.  The value is fetched at the moment of use and
    returned to the caller; it is never cached, never written to evidence,
    never put in a bus message or task metadata, and never logged.  Failures
    return an empty string rather than an error carrying a partial credential.

    The secret's ``capabilities`` are the authorization signal: a credential
    that does not claim this host's capability is refused loudly instead of
    being sent to the API to come back as a confusing 401.
    """
    name = _FORGE_SECRET_NAMES.get(host_kind, "")
    if not name:
        return ""
    base_url = (
        os.environ.get("MAC_API_URL", "").strip()
        or os.environ.get("MAC_URL", "").strip()
        or os.environ.get("MAC_HUB_URL", "").strip()
    )
    api_token = os.environ.get("MAC_API_TOKEN", "").strip()
    if not base_url:
        return ""

    from mac.http_client import HubClient, HubClientError

    client = HubClient(base_url, token=api_token or None)
    try:
        resolved = client.request("POST", "/secrets/%s/resolve" % _quote(name, safe=""))
    except HubClientError:
        return ""
    except Exception:  # noqa: BLE001 - an unreachable hub is not a crash
        return ""
    if not isinstance(resolved, dict):
        return ""
    capabilities = resolved.get("capabilities")
    if isinstance(capabilities, (list, tuple)) and capabilities:
        if host_kind not in {str(item).strip() for item in capabilities}:
            raise ValueError(
                "hub secret %s does not carry the %s capability; refusing to use it"
                % (name, host_kind)
            )
    return str(resolved.get("value") or "").strip()


def forge_token(host_kind: str) -> str:
    """The forge credential for ``host_kind``, environment first, hub second.

    Deployed workers already carry ``GH_TOKEN`` in their process environment
    (``deploy_env.build_mac_env`` writes it into ``~/.mac/mac.env``, which the
    service wrapper sources with ``set -a``; k8s workers get it as an optional
    ``secretKeyRef``), and that is the same variable ``token_for_host`` reads
    for ``guarded_push``.  When it is absent -- a worker deployed without the
    GitHub credential, or a lane that scrubbed it -- the hub's audited secret
    store answers instead.  Either way the agent, not the hub, is the party
    that calls the forge.
    """
    return token_for_host(host_kind) or forge_token_from_hub(host_kind)


def agent_pull_request(
    target: "CanonicalPublicationTarget",
    *,
    task_id: str,
    task_title: str = "",
    head_sha: str = "",
    base_sha: str = "",
) -> Dict[str, object]:
    """The agent's own pull request for the work it just pushed.

    Opening the PR is the agent's job, not the hub's: the hub is a resource
    orchestrator that records what happened and gates completion, and deciding
    how a task's own work lands belongs to the agent doing the task.  The
    result goes into the worker evidence manifest, so the hub reads the PR
    from the ledger instead of opening one itself.
    """
    title = "%s (%s)" % (
        str(task_title or "").strip() or "mac change",
        str(task_id or "").strip(),
    )
    body = (
        "Opened by the MAC agent that produced this change.\n\n"
        "- task: `%s`\n- head: `%s`\n- base at push: `%s`\n"
        % (task_id, head_sha or getattr(target, "task_head_sha", ""), base_sha)
    )
    outcome = open_pull_request_for_target(target, title=title, body=body)
    outcome.setdefault("task_id", str(task_id or ""))
    return outcome
