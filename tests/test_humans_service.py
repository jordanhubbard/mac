"""Service-layer contract tests for human principals."""

from __future__ import annotations

import pytest

from mac.humans_service import HumansService
from mac.models import Human, NotFoundError, ValidationError
from mac.services import ControlPlane
from mac.store import SQLiteStore


@pytest.fixture
def humans() -> HumansService:
    return HumansService(SQLiteStore(":memory:"))


def test_upsert_returns_hydrated_human_and_reuses_username(
    humans: HumansService,
) -> None:
    created = humans.upsert_human(
        "alice",
        email="alice@example.test",
        github_login="alice-gh",
        display_name="Alice",
        groups=["eng", "admins"],
    )
    updated = humans.upsert_human("alice", display_name="Alice Updated")

    assert isinstance(created, Human)
    assert created.username == "alice"
    assert created.groups == ["admins", "eng"]
    assert updated.id == created.id
    assert updated.display_name == "Alice Updated"


@pytest.mark.parametrize("username", ["", "-invalid", "has spaces"])
def test_upsert_rejects_invalid_username(
    humans: HumansService, username: str
) -> None:
    with pytest.raises(ValidationError):
        humans.upsert_human(username)


def test_get_by_id_and_username_share_the_same_principal(
    humans: HumansService,
) -> None:
    created = humans.upsert_human("bob")

    assert humans.get_human(created.id) == created
    assert humans.get_human_by_username("bob") == created


def test_missing_gets_raise_domain_errors(humans: HumansService) -> None:
    with pytest.raises(NotFoundError):
        humans.get_human("human_missing")
    with pytest.raises(NotFoundError):
        humans.get_human_by_username("missing")


def test_list_all_and_group_filter(humans: HumansService) -> None:
    alice = humans.upsert_human("alice", groups=["eng"])
    bob = humans.upsert_human("bob", groups=["ops"])

    assert humans.list_humans() == [alice, bob]
    assert humans.list_humans(group="eng") == [alice]
    assert humans.list_humans(group="missing") == []


def test_delete_reports_presence_and_removes_groups(humans: HumansService) -> None:
    created = humans.upsert_human("carol", groups=["eng"])

    assert humans.delete_human(created.id) is True
    assert humans.delete_human(created.id) is False
    assert humans.list_humans(group="eng") == []


@pytest.mark.parametrize(
    "anchor",
    ["dana", "dana@example.test", "dana-gh"],
)
def test_resolve_identity_chain_accepts_external_anchors(
    humans: HumansService, anchor: str
) -> None:
    created = humans.upsert_human(
        "dana", email="dana@example.test", github_login="dana-gh"
    )

    assert humans.resolve_identity_chain(anchor) == created


def test_resolve_identity_chain_accepts_id_and_rejects_unknown(
    humans: HumansService,
) -> None:
    created = humans.upsert_human("erin")

    assert humans.resolve_identity_chain(created.id) == created
    with pytest.raises(NotFoundError):
        humans.resolve_identity_chain("unknown-anchor")


def test_control_plane_exposes_thin_human_facade() -> None:
    control_plane = ControlPlane.in_memory()
    created = control_plane.register_human("frank", groups=["eng"])

    assert control_plane.get_human(created.id) == created
    assert control_plane.get_human_by_username("frank") == created
    assert control_plane.list_humans(group="eng") == [created]
    assert control_plane.delete_human(created.id) is True
    with pytest.raises(NotFoundError):
        control_plane.get_human(created.id)
