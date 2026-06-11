"""CLI test fixtures."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _mac_secret_key(monkeypatch):
    """mac CLI uses ControlPlane(SQLiteStore(...)) which needs MAC_SECRET_KEY."""
    monkeypatch.setenv("MAC_SECRET_KEY", "cli-test-key-with-at-least-32-characters")


@pytest.fixture(autouse=True)
def _no_ticket_mirror(monkeypatch):
    """Stop `mac task create/close` tests from auto-emitting real
    `.tickets/<id>.md` files into the repo. The CLI runs with cwd = the repo, so
    `tickets_mirror.tickets_dir()` resolves to the repo's `.tickets/` and every
    throwaway test task would otherwise litter it (parity-tickets-autoemit-01).
    The dedicated emit tests opt back in by deleting this var and pointing
    `tickets_dir` at a tmp directory.
    """
    monkeypatch.setenv("MAC_NO_TICKET_MIRROR", "1")
