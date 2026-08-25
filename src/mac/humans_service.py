"""Human principal service.

Owns the ``humans`` and ``human_groups`` tables: create/update, lookup, list,
and delete for Human principals plus the ``resolve_identity_chain`` helper that
finds a Human from any external identity anchor.

Follows the patterns established by ``roles_service.py`` and
``identity_service.py``.
"""

from __future__ import annotations

import re
from typing import Any, List, Optional

from mac.models import (
    Human,
    NotFoundError,
    ValidationError,
    json_loads,
    new_id,
    utcnow,
)

# ---------------------------------------------------------------------------
# Validation patterns
# ---------------------------------------------------------------------------

# username: 1-64 chars; letters, digits, underscores, hyphens; must not start
# with hyphen.
USERNAME_RE = re.compile(r"^[a-zA-Z0-9_][a-zA-Z0-9_\-]{0,63}$")

# Basic RFC-5321-style email sanity check (not exhaustive).
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# GitHub login: 1-39 chars; alphanumeric plus single hyphens, no leading/trailing hyphen.
GITHUB_LOGIN_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9\-]{0,37}[a-zA-Z0-9])?$")


class HumansService:
    """Service layer for Human principals.

    Wraps the store CRUD helpers (upsert_human, get_human,
    get_human_by_username, list_humans, delete_human) behind
    service-layer validation and exposes a resolve_identity_chain helper.
    """

    def __init__(self, store: Any) -> None:
        self.store = store

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def upsert_human(
        self,
        username: str,
        *,
        email: Optional[str] = None,
        github_login: Optional[str] = None,
        display_name: Optional[str] = None,
        groups: Optional[List[str]] = None,
        human_id: Optional[str] = None,
    ) -> Human:
        """Create or update a Human principal.

        ``username`` is required and must match ``USERNAME_RE``.
        ``email``, ``github_login``, and ``display_name`` are optional.
        ``groups`` defaults to an empty list when omitted.
        ``human_id`` may be supplied to force a specific id (idempotent upsert).

        Returns the persisted Human object.
        Raises ``ValidationError`` on invalid input.
        """
        username = (username or "").strip()
        if not username:
            raise ValidationError("username is required")
        if not USERNAME_RE.match(username):
            raise ValidationError(
                "username %r is invalid: must be 1-64 characters, "
                "start with a letter/digit/underscore, and contain only "
                "letters, digits, underscores, or hyphens" % username
            )

        if email is not None:
            email = email.strip()
            if email and not EMAIL_RE.match(email):
                raise ValidationError("email %r is not a valid email address" % email)
            email = email or None

        if github_login is not None:
            github_login = github_login.strip()
            if github_login and not GITHUB_LOGIN_RE.match(github_login):
                raise ValidationError(
                    "github_login %r is invalid: must be 1-39 alphanumeric "
                    "characters or hyphens, not starting or ending with a hyphen" % github_login
                )
            github_login = github_login or None

        display_name_value = (display_name or "").strip() or None
        groups_list = [str(g).strip() for g in (groups or []) if str(g).strip()]

        # Determine id: caller-supplied > existing row > new.
        hid = human_id
        if hid is None:
            existing = self.store.get_human_by_username(username)
            if existing is not None:
                hid = existing["id"]
        if hid is None:
            hid = new_id("human")

        now = utcnow()
        self.store.upsert_human(
            hid,
            username,
            email=email,
            github_login=github_login,
            display_name=display_name_value,
            groups=groups_list,
            created_at=now,
            updated_at=now,
        )
        return self.get_human(hid)

    def get_human(self, human_id: str) -> Human:
        """Return the Human with ``human_id``.

        Raises ``ValidationError`` when ``human_id`` is blank.
        Raises ``NotFoundError`` when no matching row exists.
        """
        human_id = (human_id or "").strip()
        if not human_id:
            raise ValidationError("human_id is required")
        row = self.store.get_human(human_id)
        if row is None:
            raise NotFoundError("human not found: %s" % human_id)
        return self._human_from_row(row)

    def get_human_by_username(self, username: str) -> Human:
        """Return the Human with ``username``.

        Raises ``ValidationError`` when ``username`` is blank.
        Raises ``NotFoundError`` when no matching row exists.
        """
        username = (username or "").strip()
        if not username:
            raise ValidationError("username is required")
        row = self.store.get_human_by_username(username)
        if row is None:
            raise NotFoundError("human not found for username: %s" % username)
        return self._human_from_row(row)

    def list_humans(self, *, group: Optional[str] = None) -> List[Human]:
        """Return all humans, optionally filtered by group membership."""
        rows = self.store.list_humans(group=group)
        return [self._human_from_row(row) for row in rows]

    def delete_human(self, human_id: str) -> bool:
        """Delete the Human with ``human_id``.

        Returns ``True`` if a row was deleted, ``False`` if not found.
        Raises ``ValidationError`` when ``human_id`` is blank.
        """
        human_id = (human_id or "").strip()
        if not human_id:
            raise ValidationError("human_id is required")
        return self.store.delete_human(human_id)

    # ------------------------------------------------------------------
    # Identity-chain resolver
    # ------------------------------------------------------------------

    def resolve_identity_chain(self, anchor: str) -> Human:
        """Look up a Human by any external identity anchor.

        Priority order:
        1. ``human_<...>`` id — direct id lookup.
        2. username — exact match in the ``humans`` table.
        3. email — exact match against the ``email`` column.
        4. github_login — exact match against the ``github_login`` column.

        Returns the first matching Human.
        Raises ``ValidationError`` when ``anchor`` is blank.
        Raises ``NotFoundError`` when no Human matches any anchor.
        """
        anchor = (anchor or "").strip()
        if not anchor:
            raise ValidationError("anchor is required for resolve_identity_chain")

        # 1. Try as a direct human id.
        if anchor.startswith("human_"):
            row = self.store.get_human(anchor)
            if row is not None:
                return self._human_from_row(row)

        # 2. Try as a username.
        row = self.store.get_human_by_username(anchor)
        if row is not None:
            return self._human_from_row(row)

        # 3. Try as an email address.
        row = self.store.query_one("SELECT * FROM humans WHERE email = ?", (anchor,))
        if row is not None:
            return self._human_from_row(row)

        # 4. Try as a GitHub login.
        row = self.store.query_one("SELECT * FROM humans WHERE github_login = ?", (anchor,))
        if row is not None:
            return self._human_from_row(row)

        raise NotFoundError("no human found for identity anchor: %s" % anchor)

    # ------------------------------------------------------------------
    # Row hydration
    # ------------------------------------------------------------------

    def _human_from_row(self, row: Any) -> Human:
        return Human(
            id=row["id"],
            username=row["username"],
            email=row["email"],
            github_login=row["github_login"],
            display_name=row["display_name"],
            groups=json_loads(row["groups"], []),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
