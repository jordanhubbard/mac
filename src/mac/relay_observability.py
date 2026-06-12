"""Optional NeMo Relay observability adapter (relay-01).

NeMo Relay (https://docs.nvidia.com/nemo/relay) is a framework-neutral agent
*execution runtime*: hierarchical scopes, managed tool/LLM calls with lifecycle
events (ATOF), middleware, and multi-backend export (ATOF / ATIF / OpenTelemetry
/ OpenInference). This module is the **seam**, not the full integration:

* It lets the control plane open Relay scopes and emit marks/events *when* the
  ``nemo_relay`` Python binding is installed, and degrades to a safe no-op when
  it is absent. Relay is pre-1.0 and is intentionally NOT a hard dependency of
  ``mac`` — nothing here imports ``nemo_relay`` at module import.

* It translates OpenShell's OCSF event records (the sandbox's allowed/denied
  network, HTTP, process, and finding stream) into the same observation shape
  the rest of ``mac`` already records, so sandbox *enforcement* decisions become
  first-class observations alongside the executor's own telemetry. This is the
  bridge between the two halves of the OpenShell + NeMo Relay design: OpenShell
  produces the security events, Relay/observability consumes them.

Enable with ``MAC_RELAY_OBSERVABILITY=1`` (and the ``nemo_relay`` package
installed). The full scope/managed-call/exporter rollout is tracked as
follow-up work; see ``docs/openshell-nemo-relay-integration.md``.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional

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


def _truthy(value: Optional[str]) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def relay_available() -> bool:
    """True if the ``nemo_relay`` Python binding can be imported."""
    try:  # importlib avoids a hard import at module load
        import importlib.util

        return importlib.util.find_spec("nemo_relay") is not None
    except Exception:  # pragma: no cover - defensive
        return False


def enabled() -> bool:
    """True only when the operator opted in AND the binding is importable."""
    return _truthy(os.environ.get("MAC_RELAY_OBSERVABILITY")) and relay_available()


def record_event(name: str, *, data: Optional[Dict[str, Any]] = None) -> bool:
    """Emit a Relay mark event. No-op (returns False) when Relay is unavailable.

    Best-effort: any failure in the pre-1.0 binding is swallowed and logged at
    debug so callers never have to guard the call.
    """
    if not enabled():
        return False
    try:  # pragma: no cover - exercised only with nemo_relay installed
        import nemo_relay  # type: ignore

        nemo_relay.scope.event(name, data=dict(data or {}))
        return True
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("relay record_event(%s) failed: %s", name, exc)
        return False


@contextmanager
def scope(name: str, scope_type: str = "Agent", **fields: Any) -> Iterator[Any]:
    """Open a Relay scope around a block of work.

    Yields the Relay scope handle when active, else ``None``. Always safe to use
    as ``with relay_observability.scope(...) as handle:`` regardless of whether
    Relay is installed.
    """
    if not enabled():
        yield None
        return
    try:  # pragma: no cover - exercised only with nemo_relay installed
        import nemo_relay  # type: ignore

        st = getattr(nemo_relay.ScopeType, scope_type, nemo_relay.ScopeType.Agent)
        with nemo_relay.scope.scope(name, st, input=dict(fields)) as handle:
            yield handle
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("relay scope(%s) failed: %s", name, exc)
        yield None


def flush() -> None:
    """Flush Relay subscribers (export is async; call before process exit)."""
    if not enabled():
        return
    try:  # pragma: no cover - exercised only with nemo_relay installed
        import nemo_relay  # type: ignore

        nemo_relay.subscribers.flush()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("relay flush failed: %s", exc)


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
