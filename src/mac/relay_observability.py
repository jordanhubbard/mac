"""NeMo Relay observability seam for MAC/Hermes.

Import-guarded seam so behaviour is fully unchanged when nemo-relay is absent.
Activate by setting MAC_RELAY_OBSERVABILITY=1 in the environment; any other
value (or absent) leaves the module in no-op mode.

Public surface
--------------
create_agent_scope(session_id)            -> context manager (sync)   [Phase 1]
relay_tool_context(tool_name, input_dict) -> context manager (sync)   [Phase 2]
relay_llm_context(model, provider, ...)   -> context manager (sync)   [Phase 2]
flush()                                   -> None
is_available()                            -> bool

All scopes are opened with ``nemo_relay.scope.scope(name, ScopeType.X,
data=...)`` and async export is drained with
``nemo_relay._native.flush_subscribers()`` — the real nemo-relay 0.3.0 API.

When nemo-relay is absent or MAC_RELAY_OBSERVABILITY != '1' every context
manager is a transparent no-op and flush() does nothing, so callers need no
conditional logic.
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any, Dict, Generator, Optional

# ---------------------------------------------------------------------------
# Optional import guard — never raises; absence becomes is_available() -> False
# ---------------------------------------------------------------------------
try:
    import nemo_relay as _nemo_relay
    from nemo_relay._native import flush_subscribers as _flush_subscribers
    _NEMO_RELAY_AVAILABLE: bool = True
except ImportError:
    _nemo_relay = None  # type: ignore[assignment]
    _flush_subscribers = None  # type: ignore[assignment]
    _NEMO_RELAY_AVAILABLE = False


def is_available() -> bool:
    """Return True when nemo-relay is importable AND MAC_RELAY_OBSERVABILITY=1."""
    return _NEMO_RELAY_AVAILABLE and _relay_enabled()


def _relay_enabled() -> bool:
    return os.environ.get("MAC_RELAY_OBSERVABILITY", "").strip() == "1"


def flush() -> None:
    """Flush pending async exports.  No-op when relay is absent/disabled."""
    if not is_available():
        return
    try:
        _flush_subscribers()  # type: ignore[misc]
    except Exception:  # noqa: BLE001 — observability must never break a task run
        pass


@contextlib.contextmanager
def create_agent_scope(session_id: str) -> Generator[None, None, None]:
    """Open a root Agent scope for the given session_id.

    Context manager — usage::

        with relay_observability.create_agent_scope(session_id):
            ... run task ...
        relay_observability.flush()

    When relay is absent or disabled the body executes without any scope
    overhead, so callers need no conditional logic.

    The scope name is ``mac.agent.<session_id>`` (truncated to 128 chars for
    safety).  If _HERMES_HOME_OVERRIDE is set in the environment it is stored
    as a scope attribute so downstream exporters can associate the trace with
    the originating Hermes home directory.
    """
    if not is_available():
        yield
        return

    nr = _nemo_relay  # type: ignore[union-attr]
    scope_name = ("mac.agent.%s" % session_id)[:128]

    # Build scope attributes — store _HERMES_HOME_OVERRIDE when present so
    # downstream consumers can correlate the trace to the hermes instance.
    hermes_home_override = os.environ.get("_HERMES_HOME_OVERRIDE", "")
    scope_data: dict = {"session_id": session_id}
    if hermes_home_override:
        scope_data["hermes_home_override"] = hermes_home_override

    # Open the scope by hand (mirrors _scoped) so this generator yields EXACTLY
    # ONCE on every path. The previous `try: with ...: yield / except: yield`
    # double-yielded when the scope's __exit__ raised — contextmanager then hit
    # "RuntimeError: generator didn't stop", 500-ing every authenticated request
    # once relay was active on the hub. Opening failure -> run unscoped; teardown
    # failure -> swallowed; body exceptions propagate (and reach __exit__).
    cm = None
    try:
        cm = nr.scope.scope(scope_name, nr.ScopeType.Agent, data=scope_data)
        cm.__enter__()
    except Exception:  # noqa: BLE001 — relay-internal failure: run unscoped
        cm = None
        yield
        return
    try:
        yield
    finally:
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:  # noqa: BLE001 — observability must never break a task run
            pass


# ---------------------------------------------------------------------------
# Phase 2 — managed tool / LLM call context managers
#
# These wrap the existing tool-dispatch and LLM-call sites in a child scope.
# They use nemo_relay.scope.scope() (the documented 0.3.0 context-manager
# primitive) rather than tools.execute()/llm.execute(), which are callback-
# based and would require restructuring the call sites. Span-style wrapping
# captures the same lifecycle without that churn.
# ---------------------------------------------------------------------------


def _sanitize_attrs(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *attrs* keeping only JSON-serialisable scalar values.

    SDK client objects, file handles, coroutines, and other non-serialisable
    values are dropped — Relay attribute payloads must be JSON-serialisable.
    Lists/tuples are kept only when every element is itself a primitive.
    """
    safe: Dict[str, Any] = {}
    for k, v in attrs.items():
        if isinstance(v, (str, int, float, bool)) or v is None:
            safe[k] = v
        elif isinstance(v, (list, tuple)):
            items = list(v)
            if all(isinstance(x, (str, int, float, bool, type(None))) for x in items):
                safe[k] = items
    return safe


@contextlib.contextmanager
def _scoped(name: str, scope_type: Any, data: Dict[str, Any]) -> Generator[None, None, None]:
    """Open a child scope around the body, best-effort.

    No-op yield if relay can't open the scope (so observability never breaks a
    run). Body exceptions propagate normally (never suppressed) and are passed
    to the scope's ``__exit__`` so a failed call is recorded as failed.
    """
    nr = _nemo_relay  # type: ignore[union-attr]
    cm = None
    try:
        cm = nr.scope.scope(name[:128], scope_type, data=data)
        cm.__enter__()
    except Exception:  # noqa: BLE001 — relay-internal failure: run unscoped
        cm = None
        yield
        return
    try:
        yield
    finally:
        try:
            cm.__exit__(*sys.exc_info())
        except Exception:  # noqa: BLE001
            pass


@contextlib.contextmanager
def relay_tool_context(
    tool_name: str,
    input_dict: Optional[Dict[str, Any]] = None,
) -> Generator[None, None, None]:
    """Wrap a single tool dispatch in a ``ScopeType.Tool`` scope (no-op if
    relay is unavailable/disabled)."""
    if not is_available():
        yield
        return
    data: Dict[str, Any] = {"tool_name": tool_name}
    if input_dict:
        data.update(_sanitize_attrs(input_dict))
    with _scoped("mac.tool.%s" % tool_name, _nemo_relay.ScopeType.Tool, data):  # type: ignore[union-attr]
        yield


@contextlib.contextmanager
def relay_llm_context(
    model: str,
    provider: str,
    tokens: Optional[int] = None,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> Generator[None, None, None]:
    """Wrap an LLM provider call in a ``ScopeType.Llm`` scope (no-op if relay is
    unavailable/disabled)."""
    if not is_available():
        yield
        return
    data: Dict[str, Any] = {"model": model or "", "provider": provider or ""}
    if tokens is not None:
        data["request_tokens"] = int(tokens)
    if extra:
        data.update(_sanitize_attrs(extra))
    with _scoped("mac.llm.%s" % (model or provider), _nemo_relay.ScopeType.Llm, data):  # type: ignore[union-attr]
        yield


__all__ = [
    "is_available",
    "create_agent_scope",
    "relay_tool_context",
    "relay_llm_context",
    "flush",
]
