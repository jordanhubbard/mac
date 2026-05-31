"""Bootstrap for the vendored Hermes runtime (ADR 0001, hu-02/hu-03).

The vendored snapshot lives in ``src/mac/_hermes/`` as Hermes' original flat
top-level packages (``agent``, ``gateway``, ``hermes_cli``, ``tools``,
``plugins``, ``providers``, ``hermes_constants``, ...). Hermes imports its own
code with top-level absolute imports, so putting that directory on ``sys.path``
makes the whole runtime importable **unchanged** — no namespace rewriting (see
``deploy/hermes/SNAPSHOT.md`` "Vendor strategy").

``ensure_on_path()`` is the single entry point: call it before importing any
vendored Hermes module (e.g. the in-process gateway in hu-03).
"""

from __future__ import annotations

import os
import sys

VENDOR_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_hermes")


def is_vendored() -> bool:
    """True if a vendored snapshot is present (it is git-tracked but large)."""
    return os.path.isdir(VENDOR_DIR) and os.path.exists(os.path.join(VENDOR_DIR, "SNAPSHOT_PIN"))


def snapshot_pin() -> str:
    """Return the vendored upstream commit pin, or '' if not vendored."""
    path = os.path.join(VENDOR_DIR, "SNAPSHOT_PIN")
    try:
        for line in open(path, encoding="utf-8"):
            if line.startswith("commit "):
                return line.split(None, 1)[1].strip()
    except OSError:
        pass
    return ""


def ensure_on_path() -> str:
    """Put the vendored Hermes tree on ``sys.path`` (idempotent). Returns the dir.

    Raises ``RuntimeError`` if no snapshot is vendored, so callers fail loudly
    rather than importing a stale system-wide Hermes.
    """
    if not is_vendored():
        raise RuntimeError(
            "Hermes runtime not vendored at %s; run scripts/vendor-hermes-snapshot.sh --apply"
            % VENDOR_DIR
        )
    if VENDOR_DIR not in sys.path:
        sys.path.insert(0, VENDOR_DIR)
    return VENDOR_DIR
