from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field as dataclass_field
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import quote as _quote, urlparse, urlsplit, urlunsplit


@dataclass
class PullRequestResult:
    host: str
    number: int
    url: str
    state: str


_GIT_REMOTE_URL_RE = re.compile(
    r"^(?:https?://|ssh://|git://|file://|git@|/)[A-Za-z0-9._\-:/@%+~?=&]*$"
)
_GIT_REF_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")
_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


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
        return (
            os.environ.get("GITEA_TOKEN", "").strip()
            or os.environ.get(fallback_env, "").strip()
        )
    return os.environ.get(fallback_env, "").strip()


def inject_git_remote_auth(url: str) -> str:
    """Inject ``x-access-token:<pat>`` into an https git remote URL.

    Single source of truth for token-rewriting — the K8s clone wrapper,
    host-mode worker, and any other call sites should route through
    here. Non-https URLs are returned unchanged (auth handled by SSH
    keys / filesystem).
    """
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


def _run_git(worktree: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    argv = ["git", *args]
    try:
        return subprocess.run(
            argv,
            cwd=str(worktree),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(argv, 127, "", str(exc))


def _git_failure(proc: subprocess.CompletedProcess[str], fallback: str) -> str:
    detail = (proc.stderr or proc.stdout or "").strip() or fallback
    return redact_git_remote_auth_in_text(detail)


def resolve_canonical_publication_target(
    *,
    worktree: Path,
    canonical_remote: str,
    canonical_branch: str,
    destination_branch: str,
    prepared_base_sha: str,
    isolation_key: str,
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
    prepared_commit = _run_git(
        root, ["rev-parse", "--verify", "%s^{commit}" % prepared]
    )
    if prepared_commit.returncode != 0 or prepared_commit.stdout.strip() != prepared:
        raise ValueError("prepared repository base is not a commit: %s" % prepared)

    head = _run_git(root, ["rev-parse", "--verify", "HEAD^{commit}"])
    task_head = head.stdout.strip()
    if head.returncode != 0 or not _GIT_SHA_RE.fullmatch(task_head):
        raise ValueError(
            "could not resolve task HEAD: %s" % _git_failure(head, "invalid SHA")
        )
    prepared_ancestor = _run_git(
        root, ["merge-base", "--is-ancestor", prepared, task_head]
    )
    if prepared_ancestor.returncode != 0:
        raise ValueError(
            "prepared base %s is not an ancestor of task HEAD %s"
            % (prepared[:12], task_head[:12])
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
            False, target, error="could not resolve task HEAD: %s" % _git_failure(head, "invalid SHA")
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
            error="prepared repository base is not a commit: %s"
            % target.prepared_base_sha,
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
            ancestor = _run_git(
                worktree, ["merge-base", "--is-ancestor", canonical_tip, head_sha]
            )
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
    pushed = _run_git(
        worktree, ["push", target.remote, "HEAD:%s" % destination_ref]
    )
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
            error="push completed but remote branch verification failed for %s"
            % destination_ref,
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


def check_canonical_freshness(
    target: CanonicalPublicationTarget,
) -> CanonicalFreshnessResult:
    """Fetch and verify canonical ancestry without publishing anything."""
    return _canonical_publication_operation(target, push=False)


def guarded_push(
    target: CanonicalPublicationTarget,
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
            False, target, head_sha=target.task_head_sha, error="could not open publication lock: %s" % exc
        )
    result: Optional[CanonicalFreshnessResult] = None
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
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
    req = urllib.request.Request(
        url, headers=headers, method="GET"
    )
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
        repo_meta = _http_get_json(
            "%s/repos/%s/%s" % (api_base, owner, repo), headers
        )
        base = str(repo_meta.get("default_branch") or "main")

    title = title or ("mac: %s" % head)
    body = body or ""

    create_url = "%s/repos/%s/%s/pulls" % (api_base, owner, repo)
    payload = {"title": title, "body": body, "head": head, "base": base}

    try:
        pr = _http_post_json(create_url, headers, payload)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "already exists" in msg or "pull request already exists" in msg or "exists for these targets" in msg:
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
            api_base, owner, repo, owner, head, base
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
        head_ref = (
            (pr.get("head") or {}).get("ref") if isinstance(pr.get("head"), dict) else None
        )
        if head_ref == head:
            return PullRequestResult(
                host=host_kind,
                number=int(pr.get("number") or 0),
                url=str(pr.get("html_url") or pr.get("url") or ""),
                state=str(pr.get("state") or "open"),
            )
    return None
