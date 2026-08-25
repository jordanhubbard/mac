"""Service-layer contract tests for HumansService.

Coverage:
- create/get/list/delete CRUD lifecycle
- ValidationError on empty username
- NotFoundError on missing id
- resolve_identity_chain by id/username/email/github_login
- group filtering in list_humans
- delete cascade (human_groups rows removed)
"""

from __future__ import annotations

import pytest

from mac.models import Human, NotFoundError, ValidationError
from mac.services import ControlPlane


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def cp() -> ControlPlane:
    """Fresh in-memory ControlPlane for every test."""
    return ControlPlane.in_memory()


# ---------------------------------------------------------------------------
# CRUD lifecycle
# ---------------------------------------------------------------------------


def test_create_human_returns_hydrated_object(cp: ControlPlane) -> None:
    human = cp.register_human(
        "alice",
        email="alice@example.test",
        github_login="alice-gh",
        display_name="Alice",
        groups=["eng", "admins"],
    )
    assert isinstance(human, Human)
    assert human.username == "alice"
    assert human.email == "alice@example.test"
    assert human.github_login == "alice-gh"
    assert human.display_name == "Alice"
    assert human.id.startswith("human_")
    assert sorted(human.groups) == ["admins", "eng"]


def test_get_human_by_id(cp: ControlPlane) -> None:
    created = cp.register_human("bob")
    fetched = cp.get_human(created.id)
    assert fetched == created


def test_get_human_by_username(cp: ControlPlane) -> None:
    created = cp.register_human("carol")
    fetched = cp.get_human_by_username("carol")
    assert fetched == created


def test_update_human_upsert_reuses_id(cp: ControlPlane) -> None:
    created = cp.register_human("dana", display_name="Dana")
    updated = cp.register_human("dana", display_name="Dana Updated")
    assert updated.id == created.id
    assert updated.display_name == "Dana Updated"


def test_list_humans_returns_all(cp: ControlPlane) -> None:
    alice = cp.register_human("alice")
    bob = cp.register_human("bob")
    assert cp.list_humans() == [alice, bob]


def test_delete_human_returns_true_then_false(cp: ControlPlane) -> None:
    human = cp.register_human("erin")
    assert cp.delete_human(human.id) is True
    assert cp.delete_human(human.id) is False


def test_delete_human_removes_from_list(cp: ControlPlane) -> None:
    human = cp.register_human("frank")
    cp.delete_human(human.id)
    assert cp.list_humans() == []


# ---------------------------------------------------------------------------
# Validation: empty/invalid username
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("username", ["", "-invalid", "has spaces"])
def test_empty_or_invalid_username_raises_validation_error(cp: ControlPlane, username: str) -> None:
    with pytest.raises(ValidationError):
        cp.register_human(username)


# ---------------------------------------------------------------------------
# NotFoundError on missing id
# ---------------------------------------------------------------------------


def test_get_missing_id_raises_not_found(cp: ControlPlane) -> None:
    with pytest.raises(NotFoundError):
        cp.get_human("human_doesnotexist")


def test_get_missing_username_raises_not_found(cp: ControlPlane) -> None:
    with pytest.raises(NotFoundError):
        cp.get_human_by_username("nobody")


# ---------------------------------------------------------------------------
# resolve_identity_chain
# ---------------------------------------------------------------------------


def test_resolve_identity_chain_by_id(cp: ControlPlane) -> None:
    created = cp.register_human("grace")
    assert cp.humans.resolve_identity_chain(created.id) == created


def test_resolve_identity_chain_by_username(cp: ControlPlane) -> None:
    created = cp.register_human("hank")
    assert cp.humans.resolve_identity_chain("hank") == created


def test_resolve_identity_chain_by_email(cp: ControlPlane) -> None:
    created = cp.register_human("ivan", email="ivan@example.test")
    assert cp.humans.resolve_identity_chain("ivan@example.test") == created


def test_resolve_identity_chain_by_github_login(cp: ControlPlane) -> None:
    created = cp.register_human("jane", github_login="jane-gh")
    assert cp.humans.resolve_identity_chain("jane-gh") == created


def test_resolve_identity_chain_unknown_raises_not_found(cp: ControlPlane) -> None:
    with pytest.raises(NotFoundError):
        cp.humans.resolve_identity_chain("unknown-anchor")


# ---------------------------------------------------------------------------
# Group filtering in list_humans
# ---------------------------------------------------------------------------


def test_list_humans_group_filter(cp: ControlPlane) -> None:
    alice = cp.register_human("alice", groups=["eng"])
    bob = cp.register_human("bob", groups=["ops"])
    _carol = cp.register_human("carol", groups=["eng", "ops"])

    eng = cp.list_humans(group="eng")
    assert alice in eng
    assert bob not in eng

    ops = cp.list_humans(group="ops")
    assert bob in ops
    assert alice not in ops


def test_list_humans_empty_group_filter(cp: ControlPlane) -> None:
    cp.register_human("alice", groups=["eng"])
    assert cp.list_humans(group="missing") == []


# ---------------------------------------------------------------------------
# Delete cascade: human_groups rows removed
# ---------------------------------------------------------------------------


def test_delete_cascade_removes_group_membership(cp: ControlPlane) -> None:
    human = cp.register_human("kyle", groups=["eng", "ops"])

    # Confirm group memberships visible before deletion.
    assert cp.list_humans(group="eng") == [human]
    assert cp.list_humans(group="ops") == [human]

    cp.delete_human(human.id)

    # After deletion the group rows must be gone.
    assert cp.list_humans(group="eng") == []
    assert cp.list_humans(group="ops") == []
