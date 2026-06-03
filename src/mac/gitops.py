from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import quote as _quote, urlparse, urlsplit, urlunsplit


@dataclass
class PullRequestResult:
    host: str
    number: int
    url: str
    state: str


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
