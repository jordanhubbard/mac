"""Optional NeMo Relay observability adapter (relay-01).

NeMo Relay (https://docs.nvidia.com/nemo/relay) is a framework-neutral agent
*execution runtime*: hierarchical scopes, managed tool/LLM calls with lifecycle
events (ATOF), middleware, and multi-backend export (ATOF / ATIF / OpenTelemetry
/ OpenInference). This module is the **seam**, not the full integration:

* It lets the control plane open Relay scopes and emit marks/events *when* the
  ``nemo_relay`` Python binding is installed, and degrades to a safe no-op when
  it is absent. Relay is pre-1.0 and is intentionally NOT a hard dependency of
  ``mac`` — nothing here imports ``nemo_relay`` at module import (the seam below
  guards the import so absence simply becomes ``is_available() -> False``).

* It exposes managed scope helpers — ``create_agent_scope`` (a root Agent scope
  per executor run / per request), ``relay_tool_context`` (a Tool child scope
  per tool dispatch), and ``relay_llm_context`` (an Llm child scope per provider
  call) — so the executor, tool dispatcher, and LLM call sites can be wrapped
  with zero conditional logic at the call site.

* It translates OpenShell's OCSF event records (the sandbox's allowed/denied
  network, HTTP, process, and finding stream) into the same observation shape
  the rest of ``mac`` already records, so sandbox *enforcement* decisions become
  first-class observations alongside the executor's own telemetry. This is the
  bridge between the two halves of the OpenShell + NeMo Relay design: OpenShell
  produces the security events, Relay/observability consumes them.

Enable with ``MAC_RELAY_OBSERVABILITY=1`` (and the ``nemo_relay`` package
installed). The full scope/managed-call/exporter rollout is tracked as
follow-up work; see ``docs/openshell-nemo-relay-integration.md``.

Public surface
--------------
is_available()                            -> bool   (relay importable AND opted-in)
enabled()                                 -> bool   (alias of is_available)
relay_available()                         -> bool   (relay importable, ignoring opt-in)
create_agent_scope(session_id)            -> context manager (sync)
relay_tool_context(tool_name, input_dict) -> context manager (sync)
relay_llm_context(model, provider, ...)   -> context manager (sync)
scope(name, scope_type, **fields)         -> context manager (legacy handle-yielding)
record_event(name, *, data)               -> bool
flush()                                   -> None
ocsf_to_observation / iter_ocsf_observations / parse_ocsf_lines  (pure translation)

When nemo-relay is absent or MAC_RELAY_OBSERVABILITY != '1' every context
manager is a transparent no-op and flush() does nothing, so callers need no
conditional logic.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
from contextlib import contextmanager
from typing import Any, Dict, Generator, Iterable, Iterator, List, Optional

logger = logging.getLogger(__name__)

# mac observation layer for sandbox-sourced events. Must satisfy the hub's layer
# regex ``^[A-Za-z0-9][A-Za-z0-9._\-/:]{0,127}$``.
SANDBOX_LAYER = "sandbox"

# OCSF class_uid -> (short name suffix). See OCSF v1.x category 4 (Network),
# 1 (System), 2 (Findings), 5 (Discovery), 6 (Application).
_OCSF_CLASS_NAMES = {
    4001: "network",
    4002: "http",
    4007: "ssh",
    1007: "process",
    2004: "finding",
    5019: "config",
    6002: "lifecycle",
}

# OCSF severity_id -> mac observation level.
_OCSF_SEVERITY_LEVELS = {
    0: "info",   # Unknown
    1: "info",   # Informational
    2: "info",   # Low
    3: "warning",  # Medium
    4: "error",  # High
    5: "critical",  # Critical
    6: "critical",  # Fatal
}

_LEVEL_RANK = {"debug": 0, "info": 1, "warning": 2, "error": 3, "critical": 4}


# ---------------------------------------------------------------------------
# Optional import guard — never raises; absence becomes is_available() -> False.
#
# Eagerly resolving the binding (and the 0.3.0 ``flush_subscribers`` native
# entry point) at import time gives the scope/tool/LLM seam a single source of
# truth (``_NEMO_RELAY_AVAILABLE`` / ``_nemo_relay``) that tests monkey-patch to
# simulate a relay-present environment without a wheel installed.
# ---------------------------------------------------------------------------
try:
    import nemo_relay as _nemo_relay
    from nemo_relay._native import flush_subscribers as _flush_subscribers
    _NEMO_RELAY_AVAILABLE: bool = True
except ImportError:
    _nemo_relay = None  # type: ignore[assignment]
    _flush_subscribers = None  # type: ignore[assignment]
    _NEMO_RELAY_AVAILABLE = False


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _relay_enabled() -> bool:
    return _truthy(os.environ.get("MAC_RELAY_OBSERVABILITY"))


def relay_available() -> bool:
    """True if the ``nemo_relay`` Python binding is importable (ignores opt-in)."""
    return _NEMO_RELAY_AVAILABLE


def is_available() -> bool:
    """True when nemo-relay is importable AND ``MAC_RELAY_OBSERVABILITY`` is truthy."""
    return _NEMO_RELAY_AVAILABLE and _relay_enabled()


# Backwards-compatible alias: the OCSF/legacy half historically called this
# ``enabled()``. Kept so existing callers/tests keep working.
def enabled() -> bool:
    """True only when the operator opted in AND the binding is importable."""
    return is_available()


def flush() -> None:
    """Flush pending async exports. No-op when relay is absent/disabled."""
    if not is_available():
        return
    try:
        _flush_subscribers()  # type: ignore[misc]
    except Exception:  # noqa: BLE001 — observability must never break a task run
        pass


def record_event(name: str, *, data: Optional[Dict[str, Any]] = None) -> bool:
    """Emit a Relay mark event. No-op (returns False) when Relay is unavailable.

    Best-effort: any failure in the pre-1.0 binding is swallowed and logged at
    debug so callers never have to guard the call.
    """
    if not is_available():
        return False
    try:  # pragma: no cover - exercised only with nemo_relay installed
        _nemo_relay.scope.event(name, data=dict(data or {}))  # type: ignore[union-attr]
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("relay record_event(%s) failed: %s", name, exc)
        return False


@contextmanager
def scope(name: str, scope_type: str = "Agent", **fields: Any) -> Iterator[Any]:
    """Open a Relay scope around a block of work, yielding the scope handle.

    Yields the Relay scope handle when active, else ``None``. Always safe to use
    as ``with relay_observability.scope(...) as handle:`` regardless of whether
    Relay is installed. This is the legacy handle-yielding helper; the managed
    ``create_agent_scope`` / ``relay_tool_context`` / ``relay_llm_context``
    helpers below are preferred for new call sites.
    """
    if not is_available():
        yield None
        return
    try:  # pragma: no cover - exercised only with nemo_relay installed
        nr = _nemo_relay  # type: ignore[union-attr]
        st = getattr(nr.ScopeType, scope_type, nr.ScopeType.Agent)
        with nr.scope.scope(name, st, input=dict(fields)) as handle:
            yield handle
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("relay scope(%s) failed: %s", name, exc)
        yield None


# ---------------------------------------------------------------------------
# Managed scope helpers — root Agent scope + managed tool / LLM child scopes.
#
# These wrap the existing executor / tool-dispatch / LLM-call sites in scopes.
# They use ``nemo_relay.scope.scope()`` (the documented 0.3.0 context-manager
# primitive) rather than tools.execute()/llm.execute(), which are callback-based
# and would require restructuring the call sites. Span-style wrapping captures
# the same lifecycle without that churn.
# ---------------------------------------------------------------------------


@contextmanager
def create_agent_scope(session_id: str) -> Generator[None, None, None]:
    """Open a root Agent scope for the given session_id.

    Context manager — usage::

        with relay_observability.create_agent_scope(session_id):
            ... run task ...
        relay_observability.flush()

    When relay is absent or disabled the body executes without any scope
    overhead, so callers need no conditional logic.

    The scope name is ``mac.agent.<session_id>`` (truncated to 128 chars for
    safety). If _HERMES_HOME_OVERRIDE is set in the environment it is stored as
    a scope attribute so downstream exporters can associate the trace with the
    originating Hermes home directory.
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


@contextmanager
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


@contextmanager
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


@contextmanager
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


# ---------------------------------------------------------------------------
# OpenShell OCSF -> mac observation translation (pure; always available)
# ---------------------------------------------------------------------------


def _bump_level(level: str, floor: str) -> str:
    """Return whichever of *level*/*floor* is more severe."""
    if _LEVEL_RANK.get(floor, 1) > _LEVEL_RANK.get(level, 1):
        return floor
    return level


def ocsf_to_observation(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Translate one OpenShell OCSF JSONL record into a mac observation dict.

    Returns a dict shaped for ``ObservabilityService.record_log`` /
    ``record_observation`` (``kind``, ``layer``, ``level``, ``name``, ``source``,
    ``detail``), or ``None`` if *record* is not a usable mapping. Pure and
    dependency-free: callable whether or not Relay/OpenShell are installed.
    """
    if not isinstance(record, dict):
        return None

    class_uid = record.get("class_uid")
    suffix = _OCSF_CLASS_NAMES.get(class_uid, "event")
    name = "sandbox.%s" % suffix

    # Severity: prefer the numeric severity_id; fall back to info.
    severity_id = record.get("severity_id")
    level = _OCSF_SEVERITY_LEVELS.get(severity_id, "info")

    # A denied/blocked enforcement decision is at least a warning regardless of
    # the record's own severity, so policy denials are never logged below WARN.
    action = str(record.get("action") or "").strip().lower()
    disposition = str(record.get("disposition") or "").strip().lower()
    if action in {"denied", "deny", "blocked"} or disposition in {"blocked", "denied"}:
        level = _bump_level(level, "warning")

    return {
        "kind": "log",
        "layer": SANDBOX_LAYER,
        "level": level,
        "name": name,
        "source": "openshell",
        "detail": record,
    }


def iter_ocsf_observations(
    records: Iterable[Any],
) -> Iterator[Dict[str, Any]]:
    """Map an iterable of OCSF records (e.g. parsed JSONL lines) to observations.

    Non-dict / unusable records are skipped so a malformed log line can't abort
    ingestion of the rest of the stream.
    """
    for rec in records:
        obs = ocsf_to_observation(rec) if isinstance(rec, dict) else None
        if obs is not None:
            yield obs


def parse_ocsf_lines(lines: Iterable[str]) -> List[Dict[str, Any]]:
    """Parse OpenShell ``*-ocsf.*.log`` JSONL lines into observation dicts.

    Blank lines and non-JSON / non-object lines are skipped (OpenShell also
    writes a human-readable shorthand log; only the JSONL stream is structured).
    """
    import json

    out: List[Dict[str, Any]] = []
    for line in lines:
        line = (line or "").strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (ValueError, TypeError):
            continue
        obs = ocsf_to_observation(rec) if isinstance(rec, dict) else None
        if obs is not None:
            out.append(obs)
    return out


__all__ = [
    # scope-export seam
    "is_available",
    "relay_available",
    "enabled",
    "create_agent_scope",
    "relay_tool_context",
    "relay_llm_context",
    "scope",
    "record_event",
    "flush",
    # OCSF translation
    "SANDBOX_LAYER",
    "ocsf_to_observation",
    "iter_ocsf_observations",
    "parse_ocsf_lines",
]
