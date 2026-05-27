"""CLI test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _mac_secret_key(monkeypatch):
    """mac CLI uses ControlPlane(SQLiteStore(...)) which needs MAC_SECRET_KEY."""
    monkeypatch.setenv("MAC_SECRET_KEY", "cli-test-key-with-at-least-32-characters")
