"""Per-agent worker bearer tokens.

Each worker authenticates to the hub with one agent-bound bearer token. The hub
stores only its sha256 hash in ``worker_credentials``; the raw token exists
once, when ``mac admin worker-token issue|rotate`` hands it to the operator (see
:mod:`mac.worker_token_cli`).

A new token starts ``pending_install`` and already authenticates, so the old
one keeps working while the operator installs the new one. Activating it
supersedes every other live version of the agent's token in the same
transaction. A failed install revokes the pending token instead.

:class:`WorkerCredentialPrincipalProvider` turns the live rows into bearer
principals for the API, and :func:`evaluate_worker_actor` decides whether a
principal may act as the agent a request names.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from mac.client_principals import (
    ClientPrincipalError,
    _parse_timestamp,
    _timestamp,
    _validate_agent_id,
)
from mac.models import json_dumps, json_loads
from mac.store import Store

WORKER_SCOPES = ("agent", "dispatch", "read", "write", "review:advance")
LIVE_STATES = ("pending_install", "active")


class WorkerCredentialError(ValueError):
    """A worker credential invariant was violated."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def _token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _worker_principal_id(agent_id: str, version: int) -> str:
    key = hashlib.sha256(agent_id.encode("utf-8")).hexdigest()[:24]
    return "worker-%s-v%04d" % (key, int(version))


def _record_from_row(row: Any, *, include_hash: bool = False) -> Dict[str, Any]:
    record = {
        key: row[key]
        for key in (
            "id",
            "agent_id",
            "credential_version",
            "token_fingerprint",
            "state",
            "issued_at",
            "expires_at",
            "activated_at",
            "revoked_at",
            "superseded_by",
            "created_by",
            "updated_at",
        )
    }
    record["credential_version"] = int(record["credential_version"])
    record["scopes"] = list(json_loads(row["scopes"], []))
    record["principal_kind"] = "worker"
    if include_hash:
        record["token_hash"] = str(row["token_hash"])
    return record


def _safe_record(record: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: value
        for key, value in record.items()
        if key != "token_hash" and value not in (None, "")
    }


def _not_expired(record: Dict[str, Any], now: Optional[datetime] = None) -> bool:
    try:
        expires = _parse_timestamp(record.get("expires_at"))
    except ClientPrincipalError:
        return False
    return bool(expires and expires > (now or _utcnow()).astimezone(timezone.utc))


def _lock_agent(conn: Any, agent_id: str, message: str) -> None:
    # Every credential change locks the agent row first, the same order
    # ControlPlane.delete_agent uses, so issuance and deletion cannot deadlock
    # or leave a live token behind a tombstone.
    locked = conn.execute(
        "UPDATE agents SET updated_at = updated_at WHERE id = ? AND deleted_at IS NULL",
        (agent_id,),
    )
    if locked.rowcount != 1:
        raise WorkerCredentialError(message)


@dataclass(frozen=True)
class WorkerCredentialIssue:
    record: Dict[str, Any]
    token: str
    worker_version: int


class WorkerCredentialLifecycle:
    """Issue, activate (superseding the rest), revoke and list worker tokens."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def issue(
        self,
        agent_id: str,
        *,
        expires_in: int = 30 * 24 * 60 * 60,
        actor: str = "operator",
    ) -> WorkerCredentialIssue:
        """Mint a ``pending_install`` token; it authenticates immediately."""

        exact_agent = _validate_agent_id(agent_id)
        ttl_seconds = int(expires_in)
        if ttl_seconds < 60:
            raise WorkerCredentialError("worker credential expires-in must be at least 60 seconds")
        token = "mac_worker_" + secrets.token_urlsafe(32)
        now = _utcnow()
        issued_at = _timestamp(now)
        with self.store.transaction() as conn:
            _lock_agent(conn, exact_agent, "worker credential requires a registered agent")
            version_row = conn.execute(
                "SELECT COALESCE(MAX(credential_version), 0) AS version "
                "FROM worker_credentials WHERE agent_id = ?",
                (exact_agent,),
            ).fetchone()
            version = int(version_row["version"] or 0) + 1
            principal_id = _worker_principal_id(exact_agent, version)
            conn.execute(
                "INSERT INTO worker_credentials ("
                "id, agent_id, credential_version, token_hash, token_fingerprint, scopes, "
                "state, issued_at, expires_at, created_by, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'pending_install', ?, ?, ?, ?)",
                (
                    principal_id,
                    exact_agent,
                    version,
                    _token_hash(token),
                    _fingerprint(token),
                    json_dumps(list(WORKER_SCOPES)),
                    issued_at,
                    _timestamp(now + timedelta(seconds=ttl_seconds)),
                    str(actor or "operator"),
                    issued_at,
                ),
            )
            row = conn.execute(
                "SELECT * FROM worker_credentials WHERE id = ?", (principal_id,)
            ).fetchone()
        return WorkerCredentialIssue(_record_from_row(row, include_hash=True), token, version)

    def _locked_pending(self, conn: Any, agent_id: str, principal_id: str) -> Dict[str, Any]:
        exact_agent = _validate_agent_id(agent_id)
        _lock_agent(conn, exact_agent, "worker agent does not exist")
        row = conn.execute(
            "SELECT * FROM worker_credentials WHERE id = ? AND agent_id = ? FOR UPDATE",
            (principal_id, exact_agent),
        ).fetchone()
        if row is None:
            raise WorkerCredentialError("worker principal does not exist")
        record = _record_from_row(row)
        if record["state"] != "pending_install" or not _not_expired(record):
            raise WorkerCredentialError("worker principal is no longer an unexpired pending issue")
        return record

    def activate(self, agent_id: str, principal_id: str) -> Dict[str, Any]:
        """Make a pending token the agent's only live one; supersede the rest."""

        with self.store.transaction() as conn:
            record = self._locked_pending(conn, agent_id, principal_id)
            now = _timestamp()
            conn.execute(
                "UPDATE worker_credentials SET state = 'active', activated_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, principal_id),
            )
            conn.execute(
                "UPDATE worker_credentials SET state = 'superseded', revoked_at = ?, "
                "superseded_by = ?, updated_at = ? WHERE agent_id = ? AND id <> ? "
                "AND state IN ('pending_install', 'active')",
                (now, principal_id, now, record["agent_id"], principal_id),
            )
        record.update(state="active", activated_at=now, updated_at=now)
        return _safe_record(record)

    def revoke(self, agent_id: str, principal_id: str) -> Dict[str, Any]:
        """Revoke a pending token whose install failed; the old one stays active."""

        with self.store.transaction() as conn:
            record = self._locked_pending(conn, agent_id, principal_id)
            now = _timestamp()
            conn.execute(
                "UPDATE worker_credentials SET state = 'revoked', revoked_at = ?, "
                "updated_at = ? WHERE id = ?",
                (now, now, principal_id),
            )
        record.update(state="revoked", revoked_at=now, updated_at=now)
        return _safe_record(record)

    def list(self, *, agent_id: str = "") -> List[Dict[str, Any]]:
        """Every credential row, without token hashes."""

        sql = "SELECT * FROM worker_credentials"
        params: tuple = ()
        if agent_id:
            sql += " WHERE agent_id = ?"
            params = (_validate_agent_id(agent_id),)
        sql += " ORDER BY agent_id, id"
        return [_safe_record(_record_from_row(row)) for row in self.store.query_all(sql, params)]


class WorkerCredentialPrincipalProvider:
    """Resolve live, unexpired worker token hashes for every API replica."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def tokens(self, *, now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
        instant = (now or _utcnow()).astimezone(timezone.utc)
        result: Dict[str, Dict[str, Any]] = {}
        rows = self.store.query_all(
            "SELECT wc.* FROM worker_credentials wc "
            "JOIN agents a ON a.id = wc.agent_id "
            "WHERE wc.state IN ('pending_install', 'active') "
            "AND wc.revoked_at IS NULL AND a.deleted_at IS NULL"
        )
        for row in rows:
            record = _record_from_row(row, include_hash=True)
            token_hash = record["token_hash"]
            if not _not_expired(record, instant) or not token_hash.startswith("sha256:"):
                continue
            result[token_hash] = {
                "scopes": record["scopes"],
                "client_id": record["id"],
                "agent_id": record["agent_id"],
                "principal_kind": "worker",
                "credential_fingerprint": record["token_fingerprint"],
                "worker_credential_version": record["credential_version"],
                "worker_credential_state": record["state"],
            }
        return result


@dataclass(frozen=True)
class WorkerActorDecision:
    allowed: bool
    reason: str


def evaluate_worker_actor(
    *, principal_agent_id: Optional[str], claimed_agent_id: str
) -> WorkerActorDecision:
    """May a principal act as ``claimed_agent_id``?

    An agent-bound token may act only as its own agent. An unbound token (a
    static ``MAC_API_TOKENS`` entry or an operator client) may name any agent;
    route scopes, not this check, decide what it may do.
    """

    if not principal_agent_id:
        return WorkerActorDecision(True, "unbound_principal")
    if principal_agent_id != claimed_agent_id:
        return WorkerActorDecision(False, "agent_principal_mismatch")
    return WorkerActorDecision(True, "agent_principal_match")
