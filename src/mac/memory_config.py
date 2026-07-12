"""Configuration shared by durable and vector-memory integrations."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Optional


QDRANT_URL_ENV_NAMES = (
    "MAC_QDRANT_URL",
    "QDRANT_URL",
    "QDRANT_ADDRESS",
    "QDRANT_FLEET_URL",
)


def configured_qdrant_url(
    explicit: Optional[str] = None,
    *,
    environ: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """Resolve the fleet Qdrant endpoint using the canonical precedence."""

    if explicit:
        return explicit
    env = os.environ if environ is None else environ
    for name in QDRANT_URL_ENV_NAMES:
        value = env.get(name)
        if value:
            return value
    return None
