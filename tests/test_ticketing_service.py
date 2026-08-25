"""TicketingCoordinator extraction: delegation + behavior preservation."""

from __future__ import annotations

from mac.services import ControlPlane
from mac.ticketing_service import TicketingCoordinator


def test_control_plane_composes_ticketing_coordinator():
    cp = ControlPlane.in_memory()
    assert isinstance(cp.ticketing, TicketingCoordinator)


def test_detect_ticketing_on_empty_repo(tmp_path):
    cp = ControlPlane.in_memory()
    result = cp.detect_ticketing(str(tmp_path))
    # No foreign source present -> no conversion offered; still a well-formed dict.
    assert isinstance(result, dict)
    assert result.get("needs_conversion") in (False, None)


def test_detect_ticketing_shim_delegates(tmp_path):
    cp = ControlPlane.in_memory()
    sentinel = {"delegated": True}
    cp.ticketing.detect_ticketing = lambda repo_path: sentinel  # type: ignore[assignment]
    assert cp.detect_ticketing(str(tmp_path)) is sentinel


def test_convert_ticketing_source_shim_delegates(tmp_path):
    cp = ControlPlane.in_memory()
    captured = {}

    def _fake(repo_path, *, project, actor="hermes", dry_run=False):
        captured.update(repo_path=repo_path, project=project, actor=actor, dry_run=dry_run)
        return {"status": "ok"}

    cp.ticketing.convert_ticketing_source = _fake  # type: ignore[assignment]
    out = cp.convert_ticketing_source(str(tmp_path), project="mac", dry_run=True)
    assert out == {"status": "ok"}
    assert captured == {
        "repo_path": str(tmp_path),
        "project": "mac",
        "actor": "hermes",
        "dry_run": True,
    }


def test_convert_no_conversion_needed_on_empty_repo(tmp_path):
    cp = ControlPlane.in_memory()
    out = cp.convert_ticketing_source(str(tmp_path), project="mac", dry_run=True)
    assert out["status"] in ("no_conversion_needed", "unknown_connector")
