"""Fleet deliberation service.

A Deliberation is opened when an agent encounters a decision it cannot make
unilaterally — typically an architectural conflict (Trigger 1) or a cross-domain
scope (Trigger 2) per the fleet's deliberation trigger protocol.

Three resolution paths:
  - authority   : agent in authorized_resolvers calls resolve()
  - unanimity   : all participants' final_position converge
  - expiry       : expires_at passes (handled by external sweeper, not this service)

Schema v3: authorized_resolvers populated at CREATE time from domain ownership,
not from creator identity at resolution time. See fleet memory key
deliberation.implementation.blueprint for the full blueprint.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from mac.models import (
    AuthorizationError,
    JsonDict,
    NotFoundError,
    ValidationError,
    json_dumps,
    json_loads,
    new_id,
    utcnow,
)


class DeliberationService:
    """Manage fleet deliberation records stored in the control plane DB."""

    def __init__(
        self,
        store: Any,
        *,
        on_resolve: Optional[Callable[[JsonDict], None]] = None,
    ) -> None:
        self.store = store
        self._on_resolve = on_resolve

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create_deliberation(
        self,
        *,
        topic: str,
        scope: str = "design",
        body: str = "",
        created_by: str,
        task_id: Optional[str] = None,
        authorized_resolvers: Optional[List[str]] = None,
        expires_at: Optional[str] = None,
    ) -> JsonDict:
        """Open a new deliberation.

        authorized_resolvers should be derived from domain ownership at call
        time (e.g. owners of conflicting architecture.* fleet memory keys).
        Leaving it empty means only the unanimity path is available.
        """
        if not topic or not topic.strip():
            raise ValidationError("topic is required")
        if not created_by or not created_by.strip():
            raise ValidationError("created_by is required")
        valid_scopes = {"design", "operations", "policy"}
        if scope not in valid_scopes:
            raise ValidationError(f"scope must be one of {sorted(valid_scopes)}")

        deliberation_id = new_id("deliberation")
        now = utcnow()
        row = {
            "id": deliberation_id,
            "task_id": task_id,
            "topic": topic.strip(),
            "scope": scope,
            "body": body,
            "conclusion": None,
            "status": "open",
            "created_by": created_by,
            "authorized_resolvers": json_dumps(authorized_resolvers or []),
            "participants": json_dumps([]),
            "arguments": json_dumps([]),
            "resolved_by": None,
            "resolution_path": None,
            "expires_at": expires_at,
            "created_at": now,
            "updated_at": now,
        }
        self.store.execute(
            """
            INSERT INTO deliberations
              (id, task_id, topic, scope, body, conclusion, status,
               created_by, authorized_resolvers, participants, arguments,
               resolved_by, resolution_path, expires_at, created_at, updated_at)
            VALUES
              (:id, :task_id, :topic, :scope, :body, :conclusion, :status,
               :created_by, :authorized_resolvers, :participants, :arguments,
               :resolved_by, :resolution_path, :expires_at, :created_at, :updated_at)
            """,
            row,
        )
        return self._get(deliberation_id)

    def add_participant(
        self,
        deliberation_id: str,
        *,
        agent_id: str,
        initial_position: str = "unknown",
    ) -> JsonDict:
        """Join a deliberation with an initial position."""
        d = self._get(deliberation_id)
        if d["status"] != "open":
            raise ValidationError("can only join an open deliberation")
        valid_positions = {"support", "oppose", "abstain", "unknown"}
        if initial_position not in valid_positions:
            raise ValidationError(f"position must be one of {sorted(valid_positions)}")

        participants = json_loads(d["participants"])
        if any(p["agent_id"] == agent_id for p in participants):
            raise ValidationError(f"agent {agent_id} is already a participant")

        from mac.models import utcnow as _now

        participants.append(
            {
                "agent_id": agent_id,
                "initial_position": initial_position,
                "final_position": initial_position,
                "vote": initial_position,
                "moved_by": None,
                "joined_at": utcnow(),
            }
        )
        self.store.execute(
            "UPDATE deliberations SET participants=:p, updated_at=:t WHERE id=:id",
            {"p": json_dumps(participants), "t": utcnow(), "id": deliberation_id},
        )
        return self._get(deliberation_id)

    def add_argument(
        self,
        deliberation_id: str,
        *,
        agent_id: str,
        claim: str,
        argument_type: str = "principle",
    ) -> JsonDict:
        """Submit an argument to a deliberation."""
        d = self._get(deliberation_id)
        if d["status"] != "open":
            raise ValidationError("can only add arguments to an open deliberation")
        valid_types = {"empirical", "precedent", "principle", "objection"}
        if argument_type not in valid_types:
            raise ValidationError(f"argument_type must be one of {sorted(valid_types)}")
        if not claim or not claim.strip():
            raise ValidationError("claim is required")

        arguments = json_loads(d["arguments"])
        arg_id = new_id("arg")
        arguments.append(
            {
                "id": arg_id,
                "agent_id": agent_id,
                "claim": claim.strip(),
                "argument_type": argument_type,
                "created_at": utcnow(),
            }
        )
        self.store.execute(
            "UPDATE deliberations SET arguments=:a, updated_at=:t WHERE id=:id",
            {"a": json_dumps(arguments), "t": utcnow(), "id": deliberation_id},
        )
        return self._get(deliberation_id)

    def update_position(
        self,
        deliberation_id: str,
        *,
        agent_id: str,
        new_position: str,
        moved_by: Optional[str] = None,
    ) -> JsonDict:
        """Update a participant's position, optionally citing an argument."""
        d = self._get(deliberation_id)
        if d["status"] != "open":
            raise ValidationError("can only update positions in an open deliberation")
        valid_positions = {"support", "oppose", "abstain", "unknown"}
        if new_position not in valid_positions:
            raise ValidationError(f"position must be one of {sorted(valid_positions)}")

        participants = json_loads(d["participants"])
        p = next((p for p in participants if p["agent_id"] == agent_id), None)
        if p is None:
            raise ValidationError(f"agent {agent_id} is not a participant")

        p["final_position"] = new_position
        p["vote"] = new_position
        if moved_by is not None:
            # Validate argument exists
            arguments = json_loads(d["arguments"])
            if not any(a["id"] == moved_by for a in arguments):
                raise ValidationError(f"argument {moved_by} not found in this deliberation")
            p["moved_by"] = moved_by

        self.store.execute(
            "UPDATE deliberations SET participants=:p, updated_at=:t WHERE id=:id",
            {"p": json_dumps(participants), "t": utcnow(), "id": deliberation_id},
        )
        return self._get(deliberation_id)

    def append_body(
        self,
        deliberation_id: str,
        *,
        agent_id: str,
        text: str,
    ) -> JsonDict:
        """Append reasoning text to the deliberation body."""
        d = self._get(deliberation_id)
        if d["status"] != "open":
            raise ValidationError("can only append to an open deliberation")
        if not text or not text.strip():
            raise ValidationError("text is required")

        separator = "\n\n" if d["body"] else ""
        new_body = d["body"] + separator + f"[{agent_id}] {text.strip()}"
        self.store.execute(
            "UPDATE deliberations SET body=:b, updated_at=:t WHERE id=:id",
            {"b": new_body, "t": utcnow(), "id": deliberation_id},
        )
        return self._get(deliberation_id)

    def resolve_deliberation(
        self,
        deliberation_id: str,
        *,
        conclusion: str,
        actor: str,
    ) -> JsonDict:
        """Resolve a deliberation via authority or unanimity path.

        Authority path: actor is in authorized_resolvers.
        Unanimity path: all participants' final_position converge to the same
        non-unknown value.
        """
        d = self._get(deliberation_id)
        if d["status"] != "open":
            raise ValidationError(f"deliberation is already {d['status']}")
        if not conclusion or not conclusion.strip():
            raise ValidationError("conclusion is required")

        authorized_resolvers = json_loads(d["authorized_resolvers"])
        participants = json_loads(d["participants"])

        resolution_path: Optional[str] = None
        if actor in authorized_resolvers:
            resolution_path = "authority"
        else:
            # Check unanimity: all participants have converged to same non-unknown position
            if participants:
                positions = {p["final_position"] for p in participants}
                if len(positions) == 1 and "unknown" not in positions:
                    resolution_path = "unanimity"
            if resolution_path is None:
                raise AuthorizationError(
                    f"actor '{actor}' is not in authorized_resolvers and "
                    "participants have not unanimously converged"
                )

        self.store.execute(
            """UPDATE deliberations
               SET conclusion=:c, status='resolved', resolved_by=:actor,
                   resolution_path=:path, updated_at=:t
               WHERE id=:id""",
            {
                "c": conclusion.strip(),
                "actor": actor,
                "path": resolution_path,
                "t": utcnow(),
                "id": deliberation_id,
            },
        )
        resolved = self._get(deliberation_id)
        if self._on_resolve is not None:
            try:
                self._on_resolve(resolved)
            except Exception:  # noqa: BLE001
                # Non-blocking: embedding failures must never abort a resolution.
                pass
        return resolved

    def withdraw_deliberation(self, deliberation_id: str, *, actor: str) -> JsonDict:
        """Withdraw (cancel) an open deliberation."""
        d = self._get(deliberation_id)
        if d["status"] != "open":
            raise ValidationError(f"deliberation is already {d['status']}")
        self.store.execute(
            """UPDATE deliberations
               SET status='withdrawn', resolved_by=:actor, updated_at=:t
               WHERE id=:id""",
            {"actor": actor, "t": utcnow(), "id": deliberation_id},
        )
        return self._get(deliberation_id)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_deliberation(self, deliberation_id: str) -> JsonDict:
        return self._get(deliberation_id)

    def list_deliberations(
        self,
        *,
        status: Optional[str] = None,
        scope: Optional[str] = None,
        task_id: Optional[str] = None,
        limit: int = 50,
    ) -> List[JsonDict]:
        where = []
        params: JsonDict = {"limit": limit}
        if status:
            where.append("status = :status")
            params["status"] = status
        if scope:
            where.append("scope = :scope")
            params["scope"] = scope
        if task_id:
            where.append("task_id = :task_id")
            params["task_id"] = task_id
        where_clause = ("WHERE " + " AND ".join(where)) if where else ""
        rows = self.store.fetchall(
            f"SELECT * FROM deliberations {where_clause} ORDER BY created_at DESC LIMIT :limit",
            params,
        )
        return [self._row_to_dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get(self, deliberation_id: str) -> JsonDict:
        row = self.store.fetchone(
            "SELECT * FROM deliberations WHERE id=:id",
            {"id": deliberation_id},
        )
        if row is None:
            raise NotFoundError(f"deliberation {deliberation_id} not found")
        return self._row_to_dict(row)

    def _row_to_dict(self, row: Any) -> JsonDict:
        d = (
            dict(row)
            if hasattr(row, "keys")
            else dict(
                zip(
                    [
                        "id",
                        "task_id",
                        "topic",
                        "scope",
                        "body",
                        "conclusion",
                        "status",
                        "created_by",
                        "authorized_resolvers",
                        "participants",
                        "arguments",
                        "resolved_by",
                        "resolution_path",
                        "expires_at",
                        "created_at",
                        "updated_at",
                    ],
                    row,
                )
            )
        )
        for key in ("authorized_resolvers", "participants", "arguments"):
            if isinstance(d.get(key), str):
                try:
                    d[key] = json_loads(d[key])
                except Exception:
                    d[key] = []
        return d
