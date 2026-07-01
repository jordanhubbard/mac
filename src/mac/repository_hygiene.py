from __future__ import annotations

import re
import subprocess
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

ACTIVE_TASK_STATES = frozenset(
    {"open", "claimed", "running", "needs_review", "reviewing"}
)


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
    ``preserve``. A duplicate or superseding task must identify its replacement
    before the ref can ever become eligible for automatic cleanup.
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
    if disposition in AUTO_CLEANUP_DISPOSITIONS and not reason:
        raise ValidationError("%s cancellation requires a reason" % disposition)

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
