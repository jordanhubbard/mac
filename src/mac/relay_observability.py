"""NeMo Relay observability seam for MAC/Hermes.

Import-guarded seam so behaviour is fully unchanged when nemo-relay is absent.
Activate by setting MAC_RELAY_OBSERVABILITY=1 in the environment; any other
value (or absent) leaves the module in no-op mode.

Public surface
--------------
create_agent_scope(session_id)  -> context manager (sync)
flush()                         -> None
is_available()                  -> bool

When nemo-relay is present and MAC_RELAY_OBSERVABILITY=1:
  - create_agent_scope opens a root ScopeType.Agent scope keyed to session_id.
  - _HERMES_HOME_OVERRIDE from the environment is stored as a scope attribute.
  - flush() calls nemo_relay._native.flush_subscribers() to drain async export.

When nemo-relay is absent or MAC_RELAY_OBSERVABILITY != '1':
  - create_agent_scope returns a no-op context manager.
  - flush() is a no-op.
  - is_available() returns False.
"""

from __future__ import annotations

import contextlib
import os
from typing import Generator

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

    try:
        with nr.scope.scope(scope_name, nr.ScopeType.Agent, data=scope_data):
            yield
    except Exception:  # noqa: BLE001 — observability must never break a task run
        yield
