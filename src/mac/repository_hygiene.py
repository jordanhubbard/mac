from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from mac.models import MACError, ValidationError


REPOSITORY_REF_LIFECYCLE_SCHEMA = "mac.repository_ref_lifecycle.v1"
REPOSITORY_REF_CLEANUP_SCHEMA = "mac.repository_ref_cleanup.v1"
DEFAULT_CLEANUP_GRACE_SECONDS = 7 * 24 * 60 * 60

CANCELLATION_DISPOSITIONS = (
    "duplicate",
    "superseded",
    "not_applicable",
    "deferred",
    "failed_attempt",
    "preserve",
)
AUTO_CLEANUP_DISPOSITIONS = frozenset(
    {"duplicate", "superseded", "not_applicable"}
)

_TASK_ID_RE = re.compile(r"^task_[0-9a-f]{32}$")
_REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MANAGED_BRANCH_RE = re.compile(
    r"^mac/(?P<agent>agent_[A-Za-z0-9._-]{1,32})/"
    r"(?P<task_id>task_[0-9a-f]{32})-"
    r"(?P<lease_id>lease_[0-9a-f]{1,24})$"
)
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_AUTHORITY_RE = re.compile(r"(?P<scheme>https?://)[^/@\s]+@", re.IGNORECASE)
_SCP_REMOTE_RE = re.compile(
    r"^(?:[^@/:]+@)?(?P<host>[^/:]+):(?P<path>[^\s]+)$"
)

ACTIVE_TASK_STATES = frozenset(
    {"open", "waiting", "claimed", "running", "needs_review", "reviewing"}
)

TERMINAL_TASK_STATES = frozenset({"completed", "failed", "cancelled"})
DISPATCHABLE_TASK_STATES = frozenset(
    {"open", "claimed", "running", "needs_review", "reviewing"}
)

_REPLACEMENT_CHAIN_DEPTH_LIMIT = 10


class RepositoryHygieneError(MACError):
    """Raised when repository-ref inspection or cleanup cannot proceed safely."""


@dataclass(frozen=True)
class ManagedRepositoryRef:
    remote: str
    branch: str
    ref: str
    sha: str
    task_id: str
    lease_id: str

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RepositoryRefAudit:
    remote: str
    branch: str
    ref: str
    sha: str
    task_id: str
    lease_id: str
    task_state: str
    disposition: str
    classification: str
    eligible: bool
    eligible_after: Optional[str]
    reason: str
    replacement_task_id: Optional[str]
    open_pull_request: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _redact(text: Any) -> str:
    return _AUTHORITY_RE.sub(r"\g<scheme><redacted>@", str(text or ""))


def redact_repository_hygiene_text(text: Any) -> str:
    """Return repository-hygiene diagnostics without URL credentials."""

    return _redact(text)


def _parse_time(value: Any) -> Optional[datetime]:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def normalize_cancellation_detail(
    detail: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    """Validate and normalize the durable cancellation contract.

    Older callers that do not know about dispositions fail closed into
    ``preserve``. Every cancellation requires an audit reason, and a duplicate
    or superseding task must identify its replacement before the ref can ever
    become eligible for automatic cleanup.
    """

    normalized = dict(detail or {})
    disposition = str(normalized.get("disposition") or "preserve").strip().lower()
    if disposition not in CANCELLATION_DISPOSITIONS:
        raise ValidationError(
            "unsupported cancellation disposition %r; expected one of %s"
            % (disposition, ", ".join(CANCELLATION_DISPOSITIONS))
        )
    reason = str(normalized.get("reason") or "").strip()
    replacement = str(normalized.get("replacement_task_id") or "").strip()
    if disposition in {"duplicate", "superseded"}:
        if not replacement or not _TASK_ID_RE.fullmatch(replacement):
            raise ValidationError(
                "%s cancellation requires replacement_task_id" % disposition
            )
    elif replacement and not _TASK_ID_RE.fullmatch(replacement):
        raise ValidationError("replacement_task_id must be a task_<32 hex> identifier")
    if not reason:
        raise ValidationError("task cancellation requires a reason (non-empty)")

    raw_grace = normalized.get(
        "cleanup_grace_seconds", DEFAULT_CLEANUP_GRACE_SECONDS
    )
    try:
        grace = int(raw_grace)
    except (TypeError, ValueError) as exc:
        raise ValidationError("cleanup_grace_seconds must be an integer") from exc
    if grace < 0 or grace > 365 * 24 * 60 * 60:
        raise ValidationError("cleanup_grace_seconds must be between 0 and 31536000")

    normalized["disposition"] = disposition
    normalized["cleanup_grace_seconds"] = grace
    if reason:
        normalized["reason"] = reason
    if replacement:
        normalized["replacement_task_id"] = replacement
    else:
        normalized.pop("replacement_task_id", None)
    return normalized


def repository_ref_lifecycle_for_transition(
    target_state: str,
    detail: Optional[Mapping[str, Any]],
    *,
    now: str,
) -> Optional[Dict[str, Any]]:
    """Return durable lifecycle metadata for a task state transition."""

    state = str(target_state or "").strip().lower()
    at = _parse_time(now)
    if at is None:
        raise ValidationError("repository-ref lifecycle requires an ISO timestamp")

    if state == "cancelled":
        normalized = normalize_cancellation_detail(detail)
        disposition = normalized["disposition"]
        grace = int(normalized["cleanup_grace_seconds"])
        if disposition in AUTO_CLEANUP_DISPOSITIONS:
            status = "scheduled"
            eligible_after = _iso(at + timedelta(seconds=grace))
        elif disposition == "failed_attempt":
            status = "quarantined"
            eligible_after = None
        else:
            status = "preserved"
            eligible_after = None
        return {
            "schema": REPOSITORY_REF_LIFECYCLE_SCHEMA,
            "task_state": state,
            "disposition": disposition,
            "status": status,
            "terminal_at": _iso(at),
            "eligible_after": eligible_after,
            "cleanup_grace_seconds": grace,
            "replacement_task_id": normalized.get("replacement_task_id"),
            "reason": str(normalized.get("reason") or ""),
        }

    if state == "completed":
        raw_grace = (detail or {}).get(
            "cleanup_grace_seconds", DEFAULT_CLEANUP_GRACE_SECONDS
        )
        try:
            grace = int(raw_grace)
        except (TypeError, ValueError) as exc:
            raise ValidationError("cleanup_grace_seconds must be an integer") from exc
        if grace < 0 or grace > 365 * 24 * 60 * 60:
            raise ValidationError(
                "cleanup_grace_seconds must be between 0 and 31536000"
            )
        return {
            "schema": REPOSITORY_REF_LIFECYCLE_SCHEMA,
            "task_state": state,
            "disposition": "integrated",
            "status": "scheduled",
            "terminal_at": _iso(at),
            "eligible_after": _iso(at + timedelta(seconds=grace)),
            "cleanup_grace_seconds": grace,
            "replacement_task_id": None,
            "reason": str((detail or {}).get("reason") or ""),
        }

    if state == "failed":
        return {
            "schema": REPOSITORY_REF_LIFECYCLE_SCHEMA,
            "task_state": state,
            "disposition": "failed_attempt",
            "status": "quarantined",
            "terminal_at": _iso(at),
            "eligible_after": None,
            "cleanup_grace_seconds": None,
            "replacement_task_id": None,
            "reason": str((detail or {}).get("reason") or ""),
        }

    if state in ACTIVE_TASK_STATES or state == "blocked":
        return {
            "schema": REPOSITORY_REF_LIFECYCLE_SCHEMA,
            "task_state": state,
            "disposition": "active" if state != "blocked" else "deferred",
            "status": "active" if state != "blocked" else "preserved",
            "terminal_at": None,
            "eligible_after": None,
            "cleanup_grace_seconds": None,
            "replacement_task_id": None,
            "reason": str((detail or {}).get("reason") or ""),
        }
    return None


def parse_managed_branch(remote: str, branch: str, sha: str) -> ManagedRepositoryRef:
    if not _REMOTE_NAME_RE.fullmatch(str(remote or "")):
        raise RepositoryHygieneError("invalid git remote name")
    match = _MANAGED_BRANCH_RE.fullmatch(str(branch or ""))
    if match is None:
        raise RepositoryHygieneError(
            "ref is outside the managed mac/agent_*/task_*-lease_* namespace"
        )
    normalized_sha = str(sha or "").strip().lower()
    if not _SHA_RE.fullmatch(normalized_sha):
        raise RepositoryHygieneError("managed ref resolved to an invalid commit SHA")
    return ManagedRepositoryRef(
        remote=remote,
        branch=branch,
        ref="refs/heads/%s" % branch,
        sha=normalized_sha,
        task_id=match.group("task_id"),
        lease_id=match.group("lease_id"),
    )


def _run(
    repo: Path,
    argv: Sequence[str],
    *,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RepositoryHygieneError("required git command is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepositoryHygieneError("git repository-ref operation timed out") from exc
    except OSError as exc:
        raise RepositoryHygieneError("git repository-ref operation failed") from exc


def query_open_pull_requests(
    repo: Path,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[Optional[Dict[str, str]], str]:
    """Return open GitHub pull requests keyed by head branch.

    Failure is represented as ``(None, warning)`` rather than an empty mapping,
    because callers must distinguish "no open pull requests" from "the check
    could not be completed" and fail closed before deletion.
    """

    try:
        result = runner(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "headRefName,number,url",
            ],
            cwd=str(Path(repo).expanduser().resolve()),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return None, "GitHub pull request state could not be verified"
    if result.returncode != 0:
        return None, "GitHub pull request state could not be verified"
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        return None, "GitHub pull request state returned malformed JSON"
    if not isinstance(payload, list):
        return None, "GitHub pull request state returned an invalid response"
    heads: Dict[str, str] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        branch = str(item.get("headRefName") or "").strip()
        if not branch:
            continue
        url = str(item.get("url") or "").strip()
        number = str(item.get("number") or "").strip()
        heads[branch] = url or (("PR #%s" % number) if number else "open pull request")
    return heads, ""


def _canonical_git_remote(value: str) -> Optional[tuple[str, str]]:
    raw = str(value or "").strip()
    if not raw:
        return None
    if "://" in raw:
        parsed = urllib.parse.urlsplit(raw)
        host = str(parsed.hostname or "").lower()
        path = str(parsed.path or "")
    else:
        match = _SCP_REMOTE_RE.fullmatch(raw)
        if match is None:
            return None
        host = match.group("host").lower()
        path = match.group("path")
    normalized_path = path.strip().strip("/")
    if normalized_path.endswith(".git"):
        normalized_path = normalized_path[:-4]
    if not host or not normalized_path:
        return None
    return host, normalized_path


def verify_repository_remote(
    repo: Path,
    remote: str,
    expected_url: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> None:
    """Fail closed unless ``remote`` matches the registered canonical URL."""

    if not _REMOTE_NAME_RE.fullmatch(str(remote or "")):
        raise RepositoryHygieneError("invalid git remote name")
    expected = _canonical_git_remote(expected_url)
    if expected is None:
        raise RepositoryHygieneError("registered canonical repository remote is invalid")
    result = runner(
        Path(repo).expanduser().resolve(),
        ["git", "remote", "get-url", remote],
        timeout=30,
    )
    if result.returncode != 0:
        raise RepositoryHygieneError("could not resolve the registered git remote")
    actual_url = next(
        (line.strip() for line in result.stdout.splitlines() if line.strip()),
        "",
    )
    actual = _canonical_git_remote(actual_url)
    if actual is None or actual != expected:
        raise RepositoryHygieneError(
            "repository remote does not match its registered canonical remote"
        )


def _validate_base_ref(
    repo: Path,
    remote: str,
    base_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> str:
    prefix = "%s/" % remote
    if not base_ref.startswith(prefix):
        raise RepositoryHygieneError(
            "canonical base ref must belong to remote %s" % remote
        )
    branch = base_ref[len(prefix) :]
    if not branch or branch.startswith("-"):
        raise RepositoryHygieneError("canonical base branch is invalid")
    checked = runner(
        repo,
        ["git", "check-ref-format", "--branch", branch],
        timeout=30,
    )
    if checked.returncode != 0:
        raise RepositoryHygieneError("canonical base branch is invalid")
    return "%s/%s" % (remote, branch)


def resolve_remote_base_ref(
    repo: Path,
    remote: str = "origin",
    *,
    configured: str = "",
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> str:
    """Resolve the remote's canonical branch, falling back safely to ``main``."""

    root = Path(repo).expanduser().resolve()
    if not _REMOTE_NAME_RE.fullmatch(str(remote or "")):
        raise RepositoryHygieneError("invalid git remote name")
    if configured:
        return _validate_base_ref(
            root,
            remote,
            str(configured).strip(),
            runner=runner,
        )

    symbolic = runner(
        root,
        [
            "git",
            "symbolic-ref",
            "--quiet",
            "--short",
            "refs/remotes/%s/HEAD" % remote,
        ],
        timeout=30,
    )
    local_ref = symbolic.stdout.strip() if symbolic.returncode == 0 else ""
    if local_ref:
        return _validate_base_ref(root, remote, local_ref, runner=runner)

    advertised = runner(
        root,
        ["git", "ls-remote", "--symref", remote, "HEAD"],
        timeout=60,
    )
    if advertised.returncode == 0:
        for line in advertised.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 3 and fields[0] == "ref:" and fields[2] == "HEAD":
                head = fields[1]
                prefix = "refs/heads/"
                if head.startswith(prefix):
                    return _validate_base_ref(
                        root,
                        remote,
                        "%s/%s" % (remote, head[len(prefix) :]),
                        runner=runner,
                    )
    return _validate_base_ref(root, remote, "%s/main" % remote, runner=runner)


def refresh_remote_base_ref(
    repo: Path,
    remote: str,
    base_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> str:
    """Fetch only the canonical branch used for merge ancestry proof."""

    root = Path(repo).expanduser().resolve()
    checked = _validate_base_ref(root, remote, base_ref, runner=runner)
    branch = checked[len(remote) + 1 :]
    fetched = runner(
        root,
        [
            "git",
            "fetch",
            "--no-tags",
            "--quiet",
            remote,
            "+refs/heads/%s:refs/remotes/%s/%s" % (branch, remote, branch),
        ],
        timeout=120,
    )
    if fetched.returncode != 0:
        raise RepositoryHygieneError(
            "could not refresh canonical base ref: %s"
            % _redact(fetched.stderr or fetched.stdout or "git fetch failed")
        )
    return checked


def list_managed_remote_refs(
    repo: Path,
    remote: str = "origin",
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
) -> List[ManagedRepositoryRef]:
    repo = Path(repo).expanduser().resolve()
    if not _REMOTE_NAME_RE.fullmatch(str(remote or "")):
        raise RepositoryHygieneError("invalid git remote name")
    result = runner(
        repo,
        [
            "git",
            "ls-remote",
            "--heads",
            remote,
            "refs/heads/mac/agent_*/task_*-lease_*",
        ],
        timeout=60,
    )
    if result.returncode != 0:
        raise RepositoryHygieneError(
            "could not inspect managed remote refs: %s"
            % _redact(result.stderr or result.stdout or "git ls-remote failed")
        )
    refs: List[ManagedRepositoryRef] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split()
        if len(fields) != 2:
            continue
        sha, ref = fields
        prefix = "refs/heads/"
        if not ref.startswith(prefix):
            continue
        try:
            refs.append(parse_managed_branch(remote, ref[len(prefix) :], sha))
        except RepositoryHygieneError:
            # The remote glob is deliberately broader than the parser. Ignore
            # lookalikes; cleanup only ever acts on exact managed refs.
            continue
    return sorted(refs, key=lambda item: (item.task_id, item.branch))


def _plain(value: Any) -> Any:
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        value = to_dict()
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _task_parts(detail: Any) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    plain = _plain(detail)
    if not isinstance(plain, dict):
        return {}, []
    task = plain.get("task") if isinstance(plain.get("task"), dict) else plain
    history = plain.get("history") if isinstance(plain.get("history"), list) else []
    return dict(task), [item for item in history if isinstance(item, dict)]


def _last_cancellation_disposition(history: Iterable[Mapping[str, Any]]) -> str:
    for item in reversed(list(history)):
        if str(item.get("to_state") or "") != "cancelled":
            continue
        detail = item.get("detail")
        if not isinstance(detail, Mapping):
            return ""
        disposition = str(detail.get("disposition") or "").strip().lower()
        return disposition if disposition in CANCELLATION_DISPOSITIONS else ""
    return ""


def _is_ancestor(
    repo: Path,
    sha: str,
    base_ref: str,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> bool:
    if not base_ref:
        return False
    result = runner(
        repo,
        ["git", "merge-base", "--is-ancestor", sha, base_ref],
        timeout=30,
    )
    return result.returncode == 0


def audit_repository_refs(
    repo: Path,
    refs: Iterable[ManagedRepositoryRef],
    task_loader: Callable[[str], Any],
    *,
    base_ref: str = "origin/main",
    now: Optional[datetime] = None,
    default_grace_seconds: int = DEFAULT_CLEANUP_GRACE_SECONDS,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
    open_pull_requests: Optional[Mapping[str, str]] = None,
) -> List[RepositoryRefAudit]:
    repo = Path(repo).expanduser().resolve()
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    loaded: Dict[str, Any] = {}
    audits: List[RepositoryRefAudit] = []
    pull_request_check_complete = open_pull_requests is not None
    pull_requests = dict(open_pull_requests or {})

    def load_task(task_id: str) -> Any:
        if task_id not in loaded:
            try:
                loaded[task_id] = task_loader(task_id)
            except Exception:  # noqa: BLE001 - unavailable task is a safe classification.
                loaded[task_id] = None
        return loaded[task_id]

    for managed in refs:
        detail = load_task(managed.task_id)
        task, history = _task_parts(detail)
        if not task:
            audits.append(
                RepositoryRefAudit(
                    **managed.to_dict(),
                    task_state="unknown",
                    disposition="unknown",
                    classification="unknown",
                    eligible=False,
                    eligible_after=None,
                    reason="task record is unavailable",
                    replacement_task_id=None,
                    open_pull_request=pull_requests.get(managed.branch),
                )
            )
            continue

        state = str(task.get("state") or "unknown").strip().lower()
        lease_id = str(task.get("lease_id") or "").strip()
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        lifecycle = (
            metadata.get("repository_ref_lifecycle")
            if isinstance(metadata.get("repository_ref_lifecycle"), dict)
            else {}
        )
        disposition = str(lifecycle.get("disposition") or "").strip().lower()
        if not disposition and state == "cancelled":
            disposition = _last_cancellation_disposition(history)
        eligible_after = str(lifecycle.get("eligible_after") or "").strip() or None
        replacement = str(lifecycle.get("replacement_task_id") or "").strip() or None
        reason = str(lifecycle.get("reason") or "").strip()
        classification = "unknown"
        eligible = False

        if state in ACTIVE_TASK_STATES or lease_id:
            classification = "active"
            disposition = disposition or "active"
            reason = "task is active or has a live lease"
        elif state == "blocked":
            classification = "blocked"
            disposition = disposition or "deferred"
            reason = reason or "blocked work is preserved"
        elif state == "failed":
            classification = "quarantined"
            disposition = disposition or "failed_attempt"
            reason = reason or "failed work requires an explicit disposition"
        elif state == "cancelled":
            disposition = disposition or "preserve"
            if disposition in AUTO_CLEANUP_DISPOSITIONS:
                classification = "superseded"
                due = _parse_time(eligible_after)
                if due is None:
                    terminal = _parse_time(task.get("completed_at"))
                    due = (
                        terminal + timedelta(seconds=default_grace_seconds)
                        if terminal is not None
                        else None
                    )
                    eligible_after = _iso(due) if due is not None else None
                eligible = due is not None and due <= current
                reason = reason or (
                    "cleanup grace period elapsed"
                    if eligible
                    else "cleanup grace period has not elapsed"
                )
                if disposition in {"duplicate", "superseded"}:
                    replacement_task, _replacement_history = (
                        _task_parts(load_task(replacement)) if replacement else ({}, [])
                    )
                    if str(replacement_task.get("state") or "") != "completed":
                        eligible = False
                        reason = "replacement task is not completed or is unavailable"
            elif disposition == "failed_attempt":
                classification = "quarantined"
                reason = reason or "failed attempt is quarantined"
            else:
                classification = "deferred"
                reason = reason or "cancellation disposition preserves the ref"
        elif state == "completed":
            disposition = disposition or "integrated"
            if _is_ancestor(repo, managed.sha, base_ref, runner=runner):
                classification = "merged"
                due = _parse_time(eligible_after)
                if due is None:
                    terminal = _parse_time(task.get("completed_at"))
                    due = (
                        terminal + timedelta(seconds=default_grace_seconds)
                        if terminal is not None
                        else None
                    )
                    eligible_after = _iso(due) if due is not None else None
                eligible = due is not None and due <= current
                reason = reason or (
                    "merged ref passed its cleanup grace period"
                    if eligible
                    else "merged ref remains inside its cleanup grace period"
                )
            else:
                classification = "unknown"
                reason = "completed task ref is not proven reachable from %s" % base_ref
        else:
            disposition = disposition or "unknown"
            reason = reason or "task state is not eligible for cleanup"

        open_pr = pull_requests.get(managed.branch)
        if open_pr:
            eligible = False
            reason = "open pull request must be closed or merged before cleanup"
        elif eligible and not pull_request_check_complete:
            eligible = False
            reason = "pull request state was not verified; refusing cleanup"

        audits.append(
            RepositoryRefAudit(
                **managed.to_dict(),
                task_state=state,
                disposition=disposition,
                classification=classification,
                eligible=eligible,
                eligible_after=eligible_after,
                reason=reason,
                replacement_task_id=replacement,
                open_pull_request=open_pr,
            )
        )
    return audits


def cleanup_evidence_metadata(
    audit: RepositoryRefAudit,
    action: str,
    *,
    at: Optional[datetime] = None,
    error: str = "",
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {
        "schema": REPOSITORY_REF_CLEANUP_SCHEMA,
        "action": action,
        "recorded_at": _iso((at or datetime.now(timezone.utc)).astimezone(timezone.utc)),
        "remote": audit.remote,
        "branch": audit.branch,
        "ref": audit.ref,
        "sha": audit.sha,
        "task_id": audit.task_id,
        "lease_id": audit.lease_id,
        "task_state": audit.task_state,
        "disposition": audit.disposition,
        "replacement_task_id": audit.replacement_task_id,
    }
    if error:
        metadata["error"] = _redact(error)
    return metadata


def _remote_sha(
    repo: Path,
    audit: RepositoryRefAudit,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> Optional[str]:
    result = runner(
        repo,
        ["git", "ls-remote", "--heads", audit.remote, audit.ref],
        timeout=60,
    )
    if result.returncode != 0:
        raise RepositoryHygieneError(
            "could not revalidate remote ref: %s"
            % _redact(result.stderr or result.stdout or "git ls-remote failed")
        )
    for line in result.stdout.splitlines():
        fields = line.split()
        if len(fields) == 2 and fields[1] == audit.ref and _SHA_RE.fullmatch(fields[0]):
            return fields[0]
    return None


def prune_repository_refs(
    repo: Path,
    audits: Iterable[RepositoryRefAudit],
    *,
    execute: bool = False,
    runner: Callable[..., subprocess.CompletedProcess[str]] = _run,
    recorder: Optional[Callable[[RepositoryRefAudit, str, str], None]] = None,
) -> Dict[str, Any]:
    """Dry-run or safely delete eligible refs using exact-SHA leases.

    The deletion push is atomic. Every candidate is re-read immediately before
    the push, and the push also carries ``--force-with-lease`` for the audited
    SHA, closing the race between revalidation and deletion.
    """

    repo = Path(repo).expanduser().resolve()
    selected = [item for item in audits if item.eligible]
    if not execute:
        return {
            "schema": REPOSITORY_REF_CLEANUP_SCHEMA,
            "mode": "dry-run",
            "eligible": [item.to_dict() for item in selected],
            "deleted": [],
            "count": len(selected),
        }
    if not selected:
        return {
            "schema": REPOSITORY_REF_CLEANUP_SCHEMA,
            "mode": "execute",
            "eligible": [],
            "deleted": [],
            "count": 0,
        }

    remotes = {item.remote for item in selected}
    if len(remotes) != 1:
        raise RepositoryHygieneError("one cleanup operation cannot span git remotes")
    for item in selected:
        parse_managed_branch(item.remote, item.branch, item.sha)
        current = _remote_sha(repo, item, runner=runner)
        if current != item.sha:
            raise RepositoryHygieneError(
                "remote ref changed after audit; refusing cleanup for %s" % item.branch
            )

    if recorder is not None:
        for item in selected:
            recorder(item, "requested", "")

    remote = selected[0].remote
    argv = ["git", "push", "--atomic", "--porcelain"]
    for item in selected:
        argv.append("--force-with-lease=%s:%s" % (item.ref, item.sha))
    argv.append(remote)
    argv.extend(":%s" % item.ref for item in selected)
    pushed = runner(repo, argv, timeout=120)
    if pushed.returncode != 0:
        error = _redact(pushed.stderr or pushed.stdout or "git push failed")
        if recorder is not None:
            for item in selected:
                recorder(item, "failed", error)
        raise RepositoryHygieneError("remote ref cleanup failed: %s" % error)

    deleted: List[Dict[str, Any]] = []
    for item in selected:
        if _remote_sha(repo, item, runner=runner) is not None:
            raise RepositoryHygieneError(
                "remote reported success but ref still exists: %s" % item.branch
            )
        runner(
            repo,
            ["git", "update-ref", "-d", "refs/remotes/%s/%s" % (item.remote, item.branch)],
            timeout=30,
        )
        if recorder is not None:
            recorder(item, "deleted", "")
        deleted.append(item.to_dict())
    return {
        "schema": REPOSITORY_REF_CLEANUP_SCHEMA,
        "mode": "execute",
        "eligible": [item.to_dict() for item in selected],
        "deleted": deleted,
        "count": len(deleted),
    }


# ---------------------------------------------------------------------------
# Replacement-liveness chain walker
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReplacementLivenessResult:
    """Result of walking a replacement_task_id chain.

    status values
    -------------
    satisfied         -- terminal task completed (work is done)
    live              -- a non-terminal dispatchable/active task exists in chain
    stranded          -- chain ends in a cancelled or failed task with no onward pointer
    held              -- chain ends in a task with metadata.no_dispatch=True
    cycle             -- a cycle was detected in the chain
    missing           -- replacement_task_id pointer is absent or unreadable
    blocked_by_terminal -- at least one dependency of a chain task is terminal (failed/cancelled)
    """

    status: str
    chain: List[str]
    remediation: str


def walk_replacement_chain(
    task_id: str,
    get_task_fn: Callable[[str], Any],
    get_task_deps_fn: Optional[Callable[[str], Any]] = None,
) -> ReplacementLivenessResult:
    """Traverse the replacement_task_id chain starting from *task_id*.

    Parameters
    ----------
    task_id:
        The ID of the root task whose replacement chain should be walked.
    get_task_fn:
        Callable that accepts a task-ID string and returns the task detail
        mapping (or raises / returns None when the task is unavailable).
    get_task_deps_fn:
        Optional callable that accepts a task-ID string and returns an
        iterable of dependency task detail mappings.  When provided, each
        chain member is checked for terminal dependencies which produce a
        ``blocked_by_terminal`` classification.
    """

    chain: List[str] = []
    seen: set = set()
    current_id: Optional[str] = task_id

    while current_id is not None:
        # Cycle detection
        if current_id in seen:
            chain.append(current_id)
            return ReplacementLivenessResult(
                status="cycle",
                chain=chain,
                remediation=(
                    "A replacement_task_id cycle was detected at %s. "
                    "Break the cycle by updating one task's replacement_task_id." % current_id
                ),
            )

        # Depth cap
        if len(chain) >= _REPLACEMENT_CHAIN_DEPTH_LIMIT:
            chain.append(current_id)
            return ReplacementLivenessResult(
                status="cycle",
                chain=chain,
                remediation=(
                    "Replacement chain exceeded the depth limit of %d. "
                    "Verify that no cycle exists and that the chain is not "
                    "unreasonably long." % _REPLACEMENT_CHAIN_DEPTH_LIMIT
                ),
            )

        seen.add(current_id)
        chain.append(current_id)

        # Load the task
        try:
            raw = get_task_fn(current_id)
        except Exception:  # noqa: BLE001
            raw = None

        task, _ = _task_parts(raw)
        if not task:
            return ReplacementLivenessResult(
                status="missing",
                chain=chain,
                remediation=(
                    "Task %s could not be loaded. "
                    "Verify that the task exists and is accessible." % current_id
                ),
            )

        state = str(task.get("state") or "").strip().lower()
        metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
        no_dispatch = bool(metadata.get("no_dispatch"))

        # Check for held status (no_dispatch) first — applies regardless of state
        if no_dispatch:
            return ReplacementLivenessResult(
                status="held",
                chain=chain,
                remediation=(
                    "Task %s has no_dispatch=True (held). "
                    "Release it with 'mac task release %s' before it can be "
                    "dispatched." % (current_id, current_id)
                ),
            )

        # Check for terminal dependencies
        if get_task_deps_fn is not None:
            try:
                deps = get_task_deps_fn(current_id) or []
            except Exception:  # noqa: BLE001
                deps = []
            for dep_raw in deps:
                dep_task, _ = _task_parts(dep_raw)
                dep_state = str(dep_task.get("state") or "").strip().lower()
                if dep_state in {"failed", "cancelled"}:
                    return ReplacementLivenessResult(
                        status="blocked_by_terminal",
                        chain=chain,
                        remediation=(
                            "Task %s has a dependency in terminal state '%s'. "
                            "The blocked task cannot make progress until that "
                            "dependency is resolved or the task is "
                            "re-queued." % (current_id, dep_state)
                        ),
                    )

        # Classify by state
        if state == "completed":
            return ReplacementLivenessResult(
                status="satisfied",
                chain=chain,
                remediation="",
            )

        if state in DISPATCHABLE_TASK_STATES:
            return ReplacementLivenessResult(
                status="live",
                chain=chain,
                remediation="",
            )

        if state in {"cancelled", "failed"}:
            # Look for an onward replacement pointer
            lifecycle = (
                metadata.get("repository_ref_lifecycle")
                if isinstance(metadata.get("repository_ref_lifecycle"), dict)
                else {}
            )
            next_id: Optional[str] = str(
                lifecycle.get("replacement_task_id") or ""
            ).strip() or None
            if next_id and _TASK_ID_RE.fullmatch(next_id):
                current_id = next_id
                continue
            return ReplacementLivenessResult(
                status="stranded",
                chain=chain,
                remediation=(
                    "Task %s is in terminal state '%s' with no onward "
                    "replacement_task_id. Create a successor task and link it "
                    "via a cancellation with disposition=superseded or "
                    "duplicate." % (current_id, state)
                ),
            )

        # Unknown / blocked / other state — treat as live to avoid false-positive stranding
        return ReplacementLivenessResult(
            status="live",
            chain=chain,
            remediation="",
        )

    # Should not be reachable (current_id starts non-None and the loop always
    # returns inside), but satisfy the type checker.
    return ReplacementLivenessResult(
        status="missing",
        chain=chain,
        remediation="Replacement chain terminated unexpectedly.",
    )


# ---------------------------------------------------------------------------
# Write guard: validate replacement target before writing
# ---------------------------------------------------------------------------


def validate_replacement_target(
    replacement_task_id: str,
    get_task_fn: Callable[[str], Any],
    *,
    archival_override: bool = False,
) -> None:
    """Raise ValidationError when the pointed-at task is already terminal or held.

    This guard prevents a superseded/duplicate cancellation from pointing at a
    task that cannot serve as a live replacement.  Pass *archival_override=True*
    only for explicit archival operations where the caller has confirmed the
    target state is intentional.

    Parameters
    ----------
    replacement_task_id:
        The task ID that will be written as the replacement pointer.
    get_task_fn:
        Callable that accepts a task-ID string and returns the task detail.
    archival_override:
        When True the guard is bypassed (for intentional archival).  Callers
        must record this flag in lifecycle metadata for audit.

    Raises
    ------
    ValidationError
        When the target is already terminal (cancelled/failed) or held
        (no_dispatch) and *archival_override* is not set.
    """

    if archival_override:
        return

    if not replacement_task_id or not _TASK_ID_RE.fullmatch(str(replacement_task_id)):
        raise ValidationError(
            "replacement_task_id must be a task_<32 hex> identifier"
        )

    try:
        raw = get_task_fn(replacement_task_id)
    except Exception:  # noqa: BLE001
        raw = None

    task, _ = _task_parts(raw)
    if not task:
        # Cannot confirm the target is terminal or held — fail open and allow
        # the write.  The chain walker can detect missing targets later.
        return

    state = str(task.get("state") or "").strip().lower()
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    no_dispatch = bool(metadata.get("no_dispatch"))

    if state in {"cancelled", "failed"}:
        raise ValidationError(
            "replacement_task_id %s is already terminal (state=%s); "
            "use archival_override=True only when this is intentional" % (replacement_task_id, state)
        )

    if no_dispatch:
        raise ValidationError(
            "replacement_task_id %s is held (no_dispatch=True); "
            "release it first or use archival_override=True if intentional" % replacement_task_id
        )
