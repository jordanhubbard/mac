"""Shared, dependency-free runtime presence projection for World4D clients."""

from datetime import datetime, timezone


def project_fields(fields, now=None):
    """A persisted online flag expires even when its publisher has disappeared."""
    result = dict(fields)
    if "runtime_valid_until" in result:
        try:
            until = datetime.fromisoformat(result["runtime_valid_until"]).timestamp()
        except (ValueError, TypeError):
            until = 0
        now = datetime.now(timezone.utc).timestamp() if now is None else now
        state = result.get("runtime_status", "unknown") if until > now else "stale"
        result["effective_runtime_status"] = state
        result["status"] = "online" if state == "online" else "offline"
    return result
