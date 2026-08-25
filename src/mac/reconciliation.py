"""Database-backed coordination for bounded control-plane reconcilers."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Optional

from mac.models import new_id, parse_time, utcnow


@dataclass(frozen=True)
class ReconciliationClaim:
    name: str
    owner_id: str
    cursor: Optional[str]


class ReconciliationCoordinator:
    """Lease one reconciler page and persist its cursor transactionally.

    The work itself deliberately runs outside the claim transaction. Each
    reconciler still uses compare-and-swap state changes, while the short lease
    prevents healthy hub replicas from duplicating the same scan page.
    """

    def __init__(
        self,
        store: Any,
        *,
        owner_id: Optional[str] = None,
        lease_seconds: Optional[int] = None,
    ) -> None:
        self.store = store
        self.owner_id = owner_id or new_id("reconciler")
        configured = (
            lease_seconds
            if lease_seconds is not None
            else os.environ.get("MAC_RECONCILER_LEASE_SECONDS", "60")
        )
        try:
            self.lease_seconds = max(1, min(int(configured), 3600))
        except (TypeError, ValueError):
            self.lease_seconds = 60

    def claim(self, name: str) -> Optional[ReconciliationClaim]:
        now = utcnow()
        claim_owner = "%s:%s" % (self.owner_id, new_id("claim"))
        expires_at = (parse_time(now) + timedelta(seconds=self.lease_seconds)).isoformat(
            timespec="microseconds"
        )
        with self.store.transaction() as conn:
            conn.execute(
                """
                INSERT INTO reconciliation_state (
                    name, cursor, lease_owner, lease_expires_at, updated_at
                ) VALUES (?, NULL, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    lease_owner = excluded.lease_owner,
                    lease_expires_at = excluded.lease_expires_at,
                    updated_at = excluded.updated_at
                WHERE reconciliation_state.lease_owner IS NULL
                   OR reconciliation_state.lease_expires_at <= excluded.updated_at
                """,
                (name, claim_owner, expires_at, now),
            )
            row = conn.execute(
                "SELECT cursor, lease_owner FROM reconciliation_state WHERE name = ?",
                (name,),
            ).fetchone()
        if row is None or row["lease_owner"] != claim_owner:
            return None
        return ReconciliationClaim(
            name=name,
            owner_id=claim_owner,
            cursor=row["cursor"],
        )

    def complete(
        self,
        claim: ReconciliationClaim,
        *,
        cursor: Optional[str],
    ) -> bool:
        changed = self.store.execute(
            """
            UPDATE reconciliation_state
            SET cursor = ?, lease_owner = NULL, lease_expires_at = NULL, updated_at = ?
            WHERE name = ? AND lease_owner = ?
            """,
            (cursor, utcnow(), claim.name, claim.owner_id),
        )
        return changed.rowcount == 1

    def abandon(self, claim: ReconciliationClaim) -> bool:
        """Release a failed page without advancing its durable cursor."""
        return self.complete(claim, cursor=claim.cursor)
