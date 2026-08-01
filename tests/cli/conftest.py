"""CLI test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _mac_secret_key(monkeypatch):
    """mac CLI uses ControlPlane(Store(...)) which needs MAC_SECRET_KEY."""
    monkeypatch.setenv("MAC_SECRET_KEY", "cli-test-key-with-at-least-32-characters")


# Note: the `_no_ticket_mirror` autouse guard now lives in the top-level
# tests/conftest.py so it covers every test dir (tests/test_dispatch.py et al.),
# not just tests/cli/. Don't re-add it here.
