"""Per-task inference tokens: model-router access for a task sandbox.

A coding CLI inside the OpenShell sandbox needs a model, and the hub's router
(``/v1``) serves it. The worker's own token cannot go into the sandbox: it can
claim tasks, write the ledger and sign evidence (see
``executor_sandbox._HOST_ONLY_HUB_CREDENTIALS``). So the worker, on the host,
mints a short-lived token bound to its agent that carries only the
``inference`` scope and hands that to the sandbox as ``MAC_INFERENCE_TOKEN``.

``inference`` grants ``POST /v1/chat/completions`` and ``POST /v1/embeddings``
and nothing else (``api._required_scope``). Because the token is bound to the
agent, the router still attributes every call to it.

The hub stores only the token's sha256 hash, in ``inference_tokens``. A token
stops authenticating when it expires or is revoked; expired rows are pruned
when the next token is minted.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Mapping, Optional
from urllib.parse import quote

from mac.client_principals import _parse_timestamp, _validate_agent_id
from mac.store import Store

INFERENCE_SCOPE = "inference"
TOKEN_PREFIX = "mac_inference_"
#: Long enough for the longest task plus margin; the executor revokes the token
#: when the task ends, so a long TTL costs nothing in the normal case.
DEFAULT_TTL_SECONDS = 6 * 60 * 60
MIN_TTL_SECONDS = 60
MAX_TTL_SECONDS = 24 * 60 * 60
#: How long an expired row is kept for audit before the next mint prunes it.
PRUNE_AFTER = timedelta(days=1)


class InferenceTokenError(ValueError):
    """An inference-token request was invalid."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(value: datetime) -> str:
    # Fixed width, so SQL text comparison orders the same as time does.
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _token_hash(token: str) -> str:
    return "sha256:" + hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class InferenceTokenIssue:
    id: str
    agent_id: str
    token: str
    expires_at: str

    def response(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "agent_id": self.agent_id,
            "token": self.token,
            "scopes": [INFERENCE_SCOPE],
            "expires_at": self.expires_at,
        }


class InferenceTokenLifecycle:
    """Mint and revoke inference tokens."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def mint(
        self,
        agent_id: str,
        *,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        task_id: str = "",
        actor: str = "",
        now: Optional[datetime] = None,
    ) -> InferenceTokenIssue:
        exact_agent = _validate_agent_id(agent_id)
        ttl = int(ttl_seconds)
        if ttl < MIN_TTL_SECONDS or ttl > MAX_TTL_SECONDS:
            raise InferenceTokenError(
                "ttl_seconds must be between %d and %d" % (MIN_TTL_SECONDS, MAX_TTL_SECONDS)
            )
        instant = (now or _utcnow()).astimezone(timezone.utc)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        token_id = "inference-" + secrets.token_hex(12)
        expires_at = _stamp(instant + timedelta(seconds=ttl))
        with self.store.transaction() as conn:
            agent = conn.execute(
                "SELECT id FROM agents WHERE id = ? AND deleted_at IS NULL",
                (exact_agent,),
            ).fetchone()
            if agent is None:
                raise InferenceTokenError("inference token requires a registered agent")
            conn.execute(
                "DELETE FROM inference_tokens WHERE expires_at < ?",
                (_stamp(instant - PRUNE_AFTER),),
            )
            conn.execute(
                "INSERT INTO inference_tokens (id, agent_id, token_hash, token_fingerprint, "
                "task_id, issued_at, expires_at, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    token_id,
                    exact_agent,
                    _token_hash(token),
                    hashlib.sha256(token.encode("utf-8")).hexdigest()[:12],
                    str(task_id or "")[:256],
                    _stamp(instant),
                    expires_at,
                    str(actor or exact_agent)[:256],
                ),
            )
        return InferenceTokenIssue(token_id, exact_agent, token, expires_at)

    def revoke(self, agent_id: str, token_id: str) -> bool:
        """Revoke one of the agent's tokens. False when no live token matched."""

        exact_agent = _validate_agent_id(agent_id)
        with self.store.transaction() as conn:
            cursor = conn.execute(
                "UPDATE inference_tokens SET revoked_at = ? "
                "WHERE id = ? AND agent_id = ? AND revoked_at IS NULL",
                (_stamp(_utcnow()), str(token_id), exact_agent),
            )
        return cursor.rowcount == 1


class InferenceTokenPrincipalProvider:
    """Resolve live inference-token hashes into inference-only principals."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def tokens(self, *, now: Optional[datetime] = None) -> Dict[str, Dict[str, Any]]:
        instant = (now or _utcnow()).astimezone(timezone.utc)
        rows = self.store.query_all(
            "SELECT it.* FROM inference_tokens it JOIN agents a ON a.id = it.agent_id "
            "WHERE it.revoked_at IS NULL AND it.expires_at > ? AND a.deleted_at IS NULL",
            (_stamp(instant),),
        )
        result: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            expires = _parse_timestamp(row["expires_at"])
            token_hash = str(row["token_hash"])
            if expires is None or expires <= instant or not token_hash.startswith("sha256:"):
                continue
            result[token_hash] = {
                "scopes": [INFERENCE_SCOPE],
                "client_id": row["id"],
                "agent_id": row["agent_id"],
                "principal_kind": "inference",
                "credential_fingerprint": row["token_fingerprint"],
                "task_id": str(row["task_id"] or "") or None,
            }
        return result


# ---------------------------------------------------------------------------
# Worker side: request a token from the hub with the worker's own credential.
# ---------------------------------------------------------------------------


def request_inference_token(
    hub_url: str,
    worker_token: str,
    agent_id: str,
    *,
    task_id: str = "",
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    timeout: float = 10.0,
) -> Dict[str, Any]:
    """POST ``/agents/{agent_id}/inference-tokens``; return the hub's JSON.

    Raises on any failure. The caller decides whether a missing token is fatal.
    """

    body = json.dumps({"task_id": task_id, "ttl_seconds": int(ttl_seconds)}).encode("utf-8")
    request = urllib.request.Request(
        "%s/agents/%s/inference-tokens" % (hub_url.rstrip("/"), quote(agent_id, safe="")),
        data=body,
        headers={
            "Authorization": "Bearer %s" % worker_token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping) or not str(payload.get("token") or "").startswith(
        TOKEN_PREFIX
    ):
        raise InferenceTokenError("hub returned no inference token")
    return dict(payload)


def revoke_inference_token(
    hub_url: str, worker_token: str, agent_id: str, token_id: str, *, timeout: float = 5.0
) -> None:
    """DELETE ``/agents/{agent_id}/inference-tokens/{token_id}``. Raises on failure."""

    request = urllib.request.Request(
        "%s/agents/%s/inference-tokens/%s"
        % (hub_url.rstrip("/"), quote(agent_id, safe=""), quote(token_id, safe="")),
        headers={"Authorization": "Bearer %s" % worker_token},
        method="DELETE",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        response.read()
