"""The vendored Slack adapter must be constructible against the REAL slack_bolt.

This exists because a library bump silently broke the Hermes gateway.

`deploy/hermes/multi-slack-mvp.patch` gives Hermes true multi-workspace Socket
Mode: one `AsyncApp` and one websocket per account from
`~/.hermes/slack_accounts.json`. It originally built each app as
`AsyncApp(token=bot_token)`. slack_bolt 1.27 constructs its request-verification
middleware eagerly and raises `signing_secret must not be empty` at that call --
before any connection is attempted -- so the gateway could not start at all.

The demand is meaningless here: Socket Mode delivers events over an
app-token-authenticated websocket. There is no inbound HTTP request to verify
and no signing secret to verify it with. `SLACK_SIGNING_SECRET` has never
appeared in any `~/.hermes/.env` backup, and Hermes served two workspaces for
months without one.

The real defect was a PINNED source fork (`src/mac/_hermes`, upstream
`b1a25404`, vendored 2026-05-31) drifting against a dependency that kept
moving. Nothing in the build caught it; it surfaced on a host.

So this module asserts BOTH halves, because either alone can pass while the
gateway is broken:

1. the vendored source still passes `request_verification_enabled=False`
   (catches a re-vendor that drops the patch), and
2. the construction the source actually performs succeeds against the INSTALLED
   slack_bolt with no signing secret in the environment (catches the next
   library bump).

The second reads its keywords out of the source rather than restating them, so
it cannot pass by testing a copy of the call that the gateway does not make.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SLACK_ADAPTER = (
    _REPO_ROOT / "src" / "mac" / "_hermes" / "gateway" / "platforms" / "slack.py"
)


def _async_app_calls() -> list[ast.Call]:
    """Every `AsyncApp(...)` construction in the vendored Slack adapter."""
    tree = ast.parse(_SLACK_ADAPTER.read_text(encoding="utf-8"))
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "AsyncApp"
    ]


def _literal_keywords(call: ast.Call) -> dict[str, object]:
    """Keyword arguments whose values are literals (tokens are not)."""
    out: dict[str, object] = {}
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        try:
            out[keyword.arg] = ast.literal_eval(keyword.value)
        except ValueError:
            continue
    return out


def test_the_adapter_still_disables_request_verification():
    """A re-vendor that drops the patch must fail here, not on a host."""
    calls = _async_app_calls()
    assert calls, (
        "no AsyncApp(...) construction found in %s -- if the adapter was "
        "restructured, this test must be repointed rather than deleted"
        % _SLACK_ADAPTER
    )
    for call in calls:
        keywords = _literal_keywords(call)
        assert keywords.get("request_verification_enabled") is False, (
            "AsyncApp is constructed at %s:%d without "
            "request_verification_enabled=False. Socket Mode has no inbound "
            "HTTP request to verify, and slack_bolt >= 1.27 raises "
            "'signing_secret must not be empty' at construction. See "
            "deploy/hermes/multi-slack-mvp.patch."
            % (_SLACK_ADAPTER.name, call.lineno)
        )


def test_that_construction_succeeds_against_the_installed_slack_bolt(monkeypatch):
    """The half that catches a library bump.

    Uses the keywords taken from the vendored source, so it exercises the call
    the gateway really makes.
    """
    # Import the ASYNC app module specifically: it pulls aiohttp at load, and
    # skipping on a missing transitive dep would quietly disarm this check.
    pytest.importorskip(
        "slack_bolt.app.async_app",
        reason="slack_bolt and aiohttp are in the dev extra; without them a "
        "version bump cannot be caught in CI",
    )
    import slack_bolt
    from slack_bolt.app.async_app import AsyncApp

    # A signing secret in the environment would mask the failure this guards.
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)

    calls = _async_app_calls()
    assert calls, "no AsyncApp(...) construction found in the vendored adapter"

    for call in calls:
        keywords = _literal_keywords(call)
        try:
            AsyncApp(token="xoxb-not-a-real-token", **keywords)
        except Exception as exc:  # pragma: no cover - the failure we guard
            pytest.fail(
                "AsyncApp(**%r) raised %s: %s against slack_bolt %s. The "
                "vendored Slack adapter cannot start with the installed "
                "library -- reconcile deploy/hermes/multi-slack-mvp.patch with "
                "the new version before deploying any Hermes gateway."
                % (
                    keywords,
                    type(exc).__name__,
                    exc,
                    getattr(slack_bolt.version, "__version__", "unknown"),
                )
            )


def test_the_signing_secret_is_not_required_by_the_construction():
    """State the property directly: no signing secret, no failure.

    If a future slack_bolt makes this impossible, the gateway needs a real
    secret from the vault and that is a deployment change, not a test fix.
    """
    pytest.importorskip("slack_bolt.app.async_app")
    from slack_bolt.app.async_app import AsyncApp

    app = AsyncApp(token="xoxb-not-a-real-token", request_verification_enabled=False)
    assert app is not None
