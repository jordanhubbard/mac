"""Freeze-notice guard for the legacy dashboard (src/mac/ui/).

The legacy dashboard is in MAINTENANCE-ONLY mode. These tests assert that the
DEPRECATED marker block is present at the top of every relevant source file,
preventing the freeze notice from being accidentally removed.

See docs/adr/0010-fleet-ide-cutover-parity-matrix.md for the rationale.
"""

from __future__ import annotations

from pathlib import Path

_UI_ROOT = Path(__file__).resolve().parents[2] / "src" / "mac" / "ui"

# The canonical marker string every frozen file must contain.
_DEPRECATED_MARKER = "DEPRECATED"
# The secondary marker pointing to the ADR.
_ADR_MARKER = "0010-fleet-ide-cutover-parity-matrix.md"
# The maintenance-only phrase used consistently in all freeze notices.
_MAINTENANCE_PHRASE = "MAINTENANCE-ONLY"


def _read(filename: str) -> str:
    return (_UI_ROOT / filename).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# app.ts — TypeScript source must carry the freeze notice
# ---------------------------------------------------------------------------


def test_app_ts_has_deprecated_marker():
    """app.ts must contain the DEPRECATED freeze-notice marker."""
    content = _read("app.ts")
    assert _DEPRECATED_MARKER in content, (
        "app.ts is missing the DEPRECATED freeze-notice block. "
        "src/mac/ui is maintenance-only — do not remove the freeze notice."
    )


def test_app_ts_has_maintenance_only_phrase():
    """app.ts freeze notice must say MAINTENANCE-ONLY."""
    assert _MAINTENANCE_PHRASE in _read("app.ts")


def test_app_ts_references_adr_0010():
    """app.ts freeze notice must reference ADR 0010."""
    assert _ADR_MARKER in _read("app.ts"), (
        "app.ts must reference docs/adr/0010-fleet-ide-cutover-parity-matrix.md"
    )


# ---------------------------------------------------------------------------
# app.js — compiled JS must carry the freeze notice
# ---------------------------------------------------------------------------


def test_app_js_has_deprecated_marker():
    """app.js must contain the DEPRECATED freeze-notice marker."""
    content = _read("app.js")
    assert _DEPRECATED_MARKER in content, (
        "app.js is missing the DEPRECATED freeze-notice block. "
        "src/mac/ui is maintenance-only — do not remove the freeze notice."
    )


def test_app_js_has_maintenance_only_phrase():
    """app.js freeze notice must say MAINTENANCE-ONLY."""
    assert _MAINTENANCE_PHRASE in _read("app.js")


def test_app_js_references_adr_0010():
    """app.js freeze notice must reference ADR 0010."""
    assert _ADR_MARKER in _read("app.js"), (
        "app.js must reference docs/adr/0010-fleet-ide-cutover-parity-matrix.md"
    )


# ---------------------------------------------------------------------------
# dashboard_api.ts — TypeScript API source must carry the freeze notice
# ---------------------------------------------------------------------------


def test_dashboard_api_ts_has_deprecated_marker():
    """dashboard_api.ts must contain the DEPRECATED freeze-notice marker."""
    content = _read("dashboard_api.ts")
    assert _DEPRECATED_MARKER in content, (
        "dashboard_api.ts is missing the DEPRECATED freeze-notice block. "
        "src/mac/ui is maintenance-only — do not remove the freeze notice."
    )


def test_dashboard_api_ts_has_maintenance_only_phrase():
    """dashboard_api.ts freeze notice must say MAINTENANCE-ONLY."""
    assert _MAINTENANCE_PHRASE in _read("dashboard_api.ts")


def test_dashboard_api_ts_references_adr_0010():
    """dashboard_api.ts freeze notice must reference ADR 0010."""
    assert _ADR_MARKER in _read("dashboard_api.ts")


# ---------------------------------------------------------------------------
# dashboard_api.js — compiled API JS must carry the freeze notice
# ---------------------------------------------------------------------------


def test_dashboard_api_js_has_deprecated_marker():
    """dashboard_api.js must contain the DEPRECATED freeze-notice marker."""
    content = _read("dashboard_api.js")
    assert _DEPRECATED_MARKER in content, (
        "dashboard_api.js is missing the DEPRECATED freeze-notice block. "
        "src/mac/ui is maintenance-only — do not remove the freeze notice."
    )


def test_dashboard_api_js_has_maintenance_only_phrase():
    """dashboard_api.js freeze notice must say MAINTENANCE-ONLY."""
    assert _MAINTENANCE_PHRASE in _read("dashboard_api.js")


def test_dashboard_api_js_references_adr_0010():
    """dashboard_api.js freeze notice must reference ADR 0010."""
    assert _ADR_MARKER in _read("dashboard_api.js")


# ---------------------------------------------------------------------------
# ADR existence check — the referenced ADR document must be present
# ---------------------------------------------------------------------------


def test_adr_0010_exists():
    """The freeze ADR docs/adr/0010-fleet-ide-cutover-parity-matrix.md must exist."""
    adr = (
        Path(__file__).resolve().parents[2]
        / "docs"
        / "adr"
        / "0010-fleet-ide-cutover-parity-matrix.md"
    )
    assert adr.exists(), (
        "docs/adr/0010-fleet-ide-cutover-parity-matrix.md is missing. "
        "The freeze notice in src/mac/ui/ references this ADR — it must be present."
    )
