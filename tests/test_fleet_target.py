"""Unit tests for the authoritative fleet target-of-record manifest."""
from __future__ import annotations

from pathlib import Path

import pytest

from mac import fleet_target as ft


def _sample() -> ft.FleetTargetManifest:
    manifest = ft.FleetTargetManifest()
    manifest.set_role(
        "gateway",
        ft.RoleTarget(
            source="0f55d49",
            openclaw=ft.OpenClawTrack(version="2026.6.11", revision="19"),
        ),
    )
    manifest.set_role("worker", ft.RoleTarget(source="abc1234def"))
    return manifest


def test_schema_constant():
    assert ft.SCHEMA == "mac.fleet_target.v1"


def test_to_dict_shape_and_sorting():
    data = _sample().to_dict()
    assert data["schema"] == "mac.fleet_target.v1"
    assert list(data["roles"]) == ["gateway", "worker"]  # sorted
    assert data["roles"]["gateway"]["source"] == "0f55d49"
    assert data["roles"]["gateway"]["openclaw"] == {
        "version": "2026.6.11",
        "revision": "19",
    }
    # Worker has no openclaw track.
    assert "openclaw" not in data["roles"]["worker"]


def test_json_roundtrip_is_stable():
    manifest = _sample()
    text = manifest.to_json()
    reparsed = ft.FleetTargetManifest.from_json(text)
    assert reparsed.to_dict() == manifest.to_dict()
    # Round-trip is idempotent at the serialized level too.
    assert reparsed.to_json() == text


def test_get_role_and_missing_role():
    manifest = _sample()
    assert manifest.get_role("gateway").openclaw.version == "2026.6.11"
    with pytest.raises(ft.FleetTargetError):
        manifest.get_role("nonexistent")


def test_rejects_unknown_schema():
    with pytest.raises(ft.FleetTargetError):
        ft.FleetTargetManifest.from_dict({"schema": "other.v1", "roles": {}})


def test_rejects_symbolic_and_bad_commit():
    with pytest.raises(ft.FleetTargetError):
        ft.RoleTarget.from_dict({"source": "HEAD"})
    with pytest.raises(ft.FleetTargetError):
        ft.RoleTarget.from_dict({"source": "zzzz"})
    with pytest.raises(ft.FleetTargetError):
        ft.RoleTarget.from_dict({"source": ""})


def test_openclaw_requires_both_fields():
    with pytest.raises(ft.FleetTargetError):
        ft.OpenClawTrack.from_dict({"version": "2026.6.11"})


def test_openclaw_revision_accepts_commit_hash():
    track = ft.OpenClawTrack.from_dict(
        {"version": "2026.6.11", "revision": "deadbeef"}
    )
    assert track.revision == "deadbeef"


def test_load_and_save_roundtrip(tmp_path: Path):
    path = tmp_path / "fleet-target.json"
    saved = ft.save_manifest(_sample(), path)
    assert saved == path
    loaded = ft.load_manifest(path)
    assert loaded.to_dict() == _sample().to_dict()


def test_load_missing_manifest_raises(tmp_path: Path):
    with pytest.raises(ft.FleetTargetError):
        ft.load_manifest(tmp_path / "nope.json")


def test_load_invalid_json_raises(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ft.FleetTargetError):
        ft.load_manifest(path)


def test_checked_in_manifest_is_valid_and_pins_both_roles():
    manifest = ft.load_manifest()  # deploy/openclaw/fleet-target.json
    assert manifest.schema == "mac.fleet_target.v1"
    gateway = manifest.get_role("gateway")
    assert gateway.openclaw is not None
    assert gateway.openclaw.version
    assert gateway.openclaw.revision
    # The worker role pins the source track.
    assert manifest.get_role("worker").source
