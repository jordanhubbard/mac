"""A client that is older than the hub should say so.

WHY THIS EXISTS. A stale client does not fail with "you are out of date"; it
fails at whatever internal seam breaks first. When the publication lane was
removed the hub stopped returning `publication_route`, an older CLI took its
backfill path, and `mac task list` died with:

    `mac task_publication_routes` is not yet supported in hub mode.
    Pass --db <path> to run against a database directly, or wait for the
    matching hub endpoint to be wrapped in RemoteDispatch.

Nothing in that names the real problem, and the suggested remedies are both
wrong for it. This turns that into one line naming both versions.

DESIGN. A response HEADER, not an endpoint: the client learns the hub's version
from a request it was already making. Skew is worth reporting; it is not worth
a round trip per command.
"""

from __future__ import annotations

import io
import sys

import pytest
from fastapi.testclient import TestClient

from mac import __version__
from mac import http_client as hc
from mac.api import create_app
from mac.services import ControlPlane


@pytest.fixture(autouse=True)
def _reset_warned():
    hc._VERSION_WARNED = False
    yield
    hc._VERSION_WARNED = False


def _stderr(monkeypatch):
    buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", buf)
    return buf


# --------------------------------------------------------------------------
# the hub advertises
# --------------------------------------------------------------------------


def test_every_response_carries_the_hub_version():
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    assert client.get("/health").headers.get("X-MAC-Version") == __version__


def test_the_header_is_on_authenticated_routes_too():
    """The auth middleware has two return paths -- a public-route early return
    and the relay-scoped one. A header on only one of them is a header the
    client sees intermittently, which is worse than none."""
    client = TestClient(create_app(control_plane=ControlPlane.in_memory()))

    resp = client.get("/tasks")

    assert resp.headers.get("X-MAC-Version") == __version__


# --------------------------------------------------------------------------
# the client notices
# --------------------------------------------------------------------------


def test_a_newer_hub_produces_one_warning(monkeypatch):
    buf = _stderr(monkeypatch)
    monkeypatch.delenv("MAC_SUPPRESS_VERSION_WARNING", raising=False)

    hc._note_hub_version("99.0.0")

    out = buf.getvalue()
    assert __version__ in out and "99.0.0" in out
    assert "make install" in out, "the warning must say what to do about it"


def test_it_warns_only_once_per_process(monkeypatch):
    buf = _stderr(monkeypatch)
    monkeypatch.delenv("MAC_SUPPRESS_VERSION_WARNING", raising=False)

    for _ in range(5):
        hc._note_hub_version("99.0.0")

    assert buf.getvalue().count("make install") == 1


def test_a_matching_version_says_nothing(monkeypatch):
    buf = _stderr(monkeypatch)

    hc._note_hub_version(__version__)

    assert buf.getvalue() == ""


def test_a_hub_that_sends_no_header_says_nothing(monkeypatch):
    """An older hub predates the header. Silence, not a false alarm."""
    buf = _stderr(monkeypatch)

    hc._note_hub_version(None)
    hc._note_hub_version("")

    assert buf.getvalue() == ""


def test_it_can_be_suppressed(monkeypatch):
    buf = _stderr(monkeypatch)
    monkeypatch.setenv("MAC_SUPPRESS_VERSION_WARNING", "1")

    hc._note_hub_version("99.0.0")

    assert buf.getvalue() == ""


def test_it_goes_to_stderr_so_json_output_stays_parseable(monkeypatch):
    """`mac --json ... | jq` must not break because the client is a version
    behind."""
    out_buf = io.StringIO()
    err_buf = _stderr(monkeypatch)
    monkeypatch.setattr(sys, "stdout", out_buf)
    monkeypatch.delenv("MAC_SUPPRESS_VERSION_WARNING", raising=False)

    hc._note_hub_version("99.0.0")

    assert out_buf.getvalue() == ""
    assert err_buf.getvalue() != ""


def test_it_never_raises(monkeypatch):
    """A version check must not be able to fail a command."""
    monkeypatch.delenv("MAC_SUPPRESS_VERSION_WARNING", raising=False)
    _stderr(monkeypatch)

    hc._note_hub_version(object())  # type: ignore[arg-type]
