"""Strict, reusable coercion for environment-backed configuration."""

from __future__ import annotations

from collections.abc import Mapping, MutableSequence
from datetime import datetime, timezone
from typing import Any, Optional


def parse_timestamp(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp, normalizing naive values to UTC."""

    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def bounded_env_number(
    environ: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
    *,
    errors: MutableSequence[str],
) -> float:
    """Read one bounded numeric setting and append actionable errors."""

    raw = str(environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        errors.append("%s must be numeric" % name)
        return default
    if value < minimum or value > maximum:
        errors.append("%s must be between %s and %s" % (name, minimum, maximum))
        return default
    return value
