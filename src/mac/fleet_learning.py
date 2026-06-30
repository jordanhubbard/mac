"""Structured, secret-free operational learning shared by the MAC fleet.

The task executor already records broad deployment outcomes.  This module
captures lower-level operational outcomes that happen before an executor can
run, beginning with repository access during review.  The records live in the
normal ``memory_records`` table so routing, vector recall, and agent prompts all
consume the same durable facts.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from mac.gitops import (
    detect_host,
    inject_git_remote_auth,
    redact_git_remote_auth,
    redact_git_remote_auth_in_text,
)


FLEET_LEARNING_SCHEMA = "mac.fleet_learning.v1"
REPOSITORY_ACCESS_KIND = "repository_access"
REPOSITORY_ACCESS_RECORD_TYPE = "fleet_learning:repository_access"
AUTH_FAILURE_CLASSES = frozenset({"authentication", "authorization"})


@dataclass(frozen=True)
class GitRemoteAccess:
    """Resolved Git access mechanism without exposing the credential value."""

    remote: str = field(repr=False)
    display: str
    host: str
    transport: str
    credential_source: str


class RepositoryAccessError(RuntimeError):
    """A classified, already-redacted repository access failure."""

    def __init__(self, message: str, *, failure_class: str) -> None:
        super().__init__(message)
        self.failure_class = failure_class


def repository_host(remote: str) -> str:
    """Return a normalized host label without retaining URL credentials."""

    value = str(remote or "").strip()
    if not value:
        return ""
    if "://" in value:
        parsed = urlsplit(value)
        if parsed.scheme == "file":
            return "local"
        return (parsed.hostname or "").lower()
    # SCP-style SSH remote, for example git@github.com:org/repo.git.
    match = re.match(r"^(?:[^@/:]+@)?([^/:]+):.+$", value)
    if match and not re.match(r"^[A-Za-z]:[\\/]", value):
        return match.group(1).lower()
    if value.startswith(("/", "./", "../", "~")):
        return "local"
    return ""


def repository_transport(remote: str) -> str:
    value = str(remote or "").strip()
    if not value:
        return "unknown"
    if "://" in value:
        scheme = urlsplit(value).scheme.lower()
        return "local" if scheme == "file" else (scheme or "unknown")
    if re.match(r"^(?:[^@/:]+@)?[^/:]+:.+$", value) and not re.match(
        r"^[A-Za-z]:[\\/]", value
    ):
        return "ssh"
    if value.startswith(("/", "./", "../", "~")):
        return "local"
    return "unknown"


def _credential_source_for_http(
    remote: str,
    environ: Mapping[str, str],
) -> str:
    parsed = urlsplit(remote)
    if parsed.username:
        return "embedded"
    try:
        host_kind = detect_host(remote)
    except ValueError:
        host_kind = "unknown"
    candidates = (
        ("GH_TOKEN", "GITHUB_TOKEN", "MAC_TASK_GIT_TOKEN")
        if host_kind == "github"
        else ("GITEA_TOKEN", "MAC_TASK_GIT_TOKEN")
    )
    for key in candidates:
        if str(environ.get(key) or "").strip():
            return "env:%s" % key
    return "ambient:https"


def resolve_git_remote_access(
    remote: str,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> GitRemoteAccess:
    """Resolve the runtime remote and a secret-free description of its auth."""

    value = str(remote or "").strip()
    env = os.environ if environ is None else environ
    transport = repository_transport(value)
    if transport in {"http", "https"}:
        credential_source = _credential_source_for_http(value, env)
    elif transport == "ssh":
        credential_source = "ssh-agent-or-key"
    elif transport == "local":
        credential_source = "local"
    elif transport == "git":
        credential_source = "anonymous"
    else:
        credential_source = "ambient:%s" % transport
    # ``inject_git_remote_auth`` intentionally reads the process environment.
    # Runtime callers use that environment; tests that pass ``environ`` only
    # inspect the classification and should not rely on a synthetic token being
    # materialized into a command argument.
    authed = inject_git_remote_auth(value) if environ is None else value
    return GitRemoteAccess(
        remote=authed,
        display=redact_git_remote_auth(authed),
        host=repository_host(value),
        transport=transport,
        credential_source=credential_source,
    )


def classify_repository_access_failure(error: str) -> str:
    """Map Git's unstable prose to a small routing-safe failure taxonomy."""

    text = str(error or "").lower()
    if any(
        marker in text
        for marker in (
            "could not read username",
            "could not read password",
            "authentication failed",
            "bad credentials",
            "invalid username or password",
            "terminal prompts disabled",
        )
    ):
        return "authentication"
    if any(
        marker in text
        for marker in (
            "permission denied",
            "not authorized",
            "authorization failed",
            "write access to repository not granted",
            "requested url returned error: 403",
        )
    ):
        return "authorization"
    if any(
        marker in text
        for marker in ("repository not found", "does not appear to be a git repository")
    ):
        return "repository_missing"
    if any(
        marker in text
        for marker in (
            "could not resolve host",
            "connection refused",
            "connection timed out",
            "network is unreachable",
            "temporary failure in name resolution",
        )
    ):
        return "network"
    if any(
        marker in text for marker in ("non-fast-forward", "fetch first", "stale info")
    ):
        return "conflict"
    return "other"


def _recommendation(
    *,
    outcome: str,
    failure_class: str,
    credential_source: str,
    host: str,
    operation: str,
) -> str:
    if outcome == "success":
        return "Prefer an agent with this recent success and reuse %s for %s on %s." % (
            credential_source,
            operation,
            host or "this repository host",
        )
    if failure_class in AUTH_FAILURE_CLASSES:
        return (
            "Do not retry this agent for %s on %s until credentials change; "
            "prefer a peer with a recent successful access learning."
            % (operation, host or "this repository host")
        )
    return "Use a recent successful peer pattern before retrying %s." % operation


def build_repository_access_learning(
    *,
    project: str,
    remote: str,
    operation: str,
    agent_id: str,
    outcome: str,
    credential_source: str,
    task_id: Optional[str] = None,
    review_id: Optional[str] = None,
    error: str = "",
    failure_class: Optional[str] = None,
    at: Optional[str] = None,
) -> dict[str, Any]:
    outcome_value = "success" if outcome == "success" else "failure"
    safe_error = redact_git_remote_auth_in_text(str(error or "")).strip()[:500]
    failure_value = (
        ""
        if outcome_value == "success"
        else (
            str(failure_class or "").strip()
            or classify_repository_access_failure(safe_error)
        )
    )
    host = repository_host(remote)
    return {
        "schema": FLEET_LEARNING_SCHEMA,
        "kind": REPOSITORY_ACCESS_KIND,
        "project": str(project or "default"),
        "repository_host": host,
        "transport": repository_transport(remote),
        "operation": str(operation or "repository_access"),
        "agent_id": str(agent_id or ""),
        "credential_source": str(credential_source or "unknown"),
        "outcome": outcome_value,
        "failure_class": failure_value,
        "error_signature": safe_error,
        "recommendation": _recommendation(
            outcome=outcome_value,
            failure_class=failure_value,
            credential_source=str(credential_source or "unknown"),
            host=host,
            operation=str(operation or "repository_access"),
        ),
        "task_id": str(task_id or "") or None,
        "review_id": str(review_id or "") or None,
        "at": at or datetime.now(timezone.utc).isoformat(timespec="microseconds"),
    }


def build_repository_access_memory_payload(
    learning: Mapping[str, Any],
) -> dict[str, Any]:
    agent_id = str(learning.get("agent_id") or "").strip()
    if not agent_id:
        raise ValueError("repository access learning requires agent_id")
    return {
        "task_id": learning.get("task_id") or None,
        "subject_type": "agent",
        "subject_id": agent_id,
        "record_type": REPOSITORY_ACCESS_RECORD_TYPE,
        "content": json.dumps(dict(learning), sort_keys=True, separators=(",", ":")),
        "evidence_id": None,
        "created_by": agent_id,
    }


def parse_repository_access_learning(content: Any) -> Optional[dict[str, Any]]:
    if isinstance(content, Mapping):
        parsed: Any = dict(content)
    else:
        try:
            parsed = json.loads(str(content or ""))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("schema") != FLEET_LEARNING_SCHEMA:
        return None
    if parsed.get("kind") != REPOSITORY_ACCESS_KIND:
        return None
    if parsed.get("outcome") not in {"success", "failure"}:
        return None
    return parsed


def _record_value(record: Any, key: str) -> Any:
    if isinstance(record, Mapping):
        return record.get(key)
    return getattr(record, key, None)


def _parse_timestamp(value: Any) -> Optional[datetime]:
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


def repository_access_state(
    records: Iterable[Any],
    *,
    project: str,
    host: str,
    operation: str,
    failure_cooldown_seconds: int,
    success_ttl_seconds: int,
    now: Optional[datetime] = None,
) -> tuple[str, Optional[dict[str, Any]]]:
    """Return ``success``, ``failure``, or ``unknown`` from newest evidence."""

    expected_project = str(project or "default")
    expected_host = str(host or "").lower()
    expected_operation = str(operation or "")
    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for record in records:
        learning = parse_repository_access_learning(_record_value(record, "content"))
        if learning is None:
            continue
        if str(learning.get("project") or "default") != expected_project:
            continue
        if str(learning.get("repository_host") or "").lower() != expected_host:
            continue
        if str(learning.get("operation") or "") != expected_operation:
            continue
        timestamp = _parse_timestamp(
            _record_value(record, "created_at")
        ) or _parse_timestamp(learning.get("at"))
        if timestamp is not None:
            candidates.append((timestamp, learning))
    if not candidates:
        return "unknown", None
    candidates.sort(key=lambda item: item[0], reverse=True)
    timestamp, learning = candidates[0]
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    age_seconds = max(0.0, (current - timestamp).total_seconds())
    if learning.get("outcome") == "success":
        if age_seconds <= max(0, int(success_ttl_seconds)):
            return "success", learning
        return "unknown", learning
    if str(
        learning.get("failure_class") or ""
    ) in AUTH_FAILURE_CLASSES and age_seconds <= max(0, int(failure_cooldown_seconds)):
        return "failure", learning
    return "unknown", learning


def task_repository_remote(task: Any) -> str:
    """Resolve a task's declared canonical remote without local-state fallback."""

    metadata = (
        task.get("metadata")
        if isinstance(task, Mapping)
        else getattr(task, "metadata", {})
    )
    if not isinstance(metadata, Mapping):
        return ""
    execution = metadata.get("execution_contract")
    origin = metadata.get("origin")
    repository_contract = metadata.get("repository_contract")
    candidates: list[Any] = []
    if isinstance(execution, Mapping):
        candidates.append(execution.get("repository_contract"))
    if isinstance(origin, Mapping):
        candidates.append(origin.get("repository_contract"))
    candidates.append(repository_contract)
    for candidate in candidates:
        if isinstance(candidate, Mapping):
            remote = str(candidate.get("canonical_remote_url") or "").strip()
            if remote:
                return remote
    if isinstance(origin, Mapping):
        return str(origin.get("repository_url") or "").strip()
    return ""
