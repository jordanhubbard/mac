"""Ratchet guard: no NEW first-party module may hard-code the ``.mac`` / ``.hermes``
home literals. Everything must resolve through ``mac.mac_paths``.

The ``_BASELINE`` set grandfathers the modules that still contain a literal as of
the home-consolidation Phase 0 landing. It must only ever SHRINK — as each site
is routed through ``mac_paths``, remove it here. Any offender NOT in the baseline
fails the build, which is how new hard-codes are prevented.

See docs/home-consolidation.md.
"""

from __future__ import annotations

import re
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src" / "mac"
_PATTERN = re.compile(r"""home\(\)\s*/\s*["']\.(mac|hermes)["']""")

# Modules that still contain a legacy literal (Phase 0 baseline; only shrink).
# Every site has been routed through mac.mac_paths, so the baseline is empty.
_BASELINE = set()


def _offenders():
    found = set()
    for path in _SRC.rglob("*.py"):
        rel = path.relative_to(_SRC).as_posix()
        if rel.startswith("_hermes/") or rel == "mac_paths.py":
            continue
        if _PATTERN.search(path.read_text(encoding="utf-8", errors="replace")):
            found.add(rel)
    return found


def test_no_new_home_literal_hardcodes():
    offenders = _offenders()
    new = sorted(offenders - _BASELINE)
    assert not new, (
        "New hard-coded .mac/.hermes home literal(s) — resolve via mac.mac_paths "
        "instead: %s" % new
    )


def test_baseline_has_no_stale_entries():
    # Keep the ratchet honest: if a file was cleaned, drop it from _BASELINE.
    offenders = _offenders()
    stale = sorted(_BASELINE - offenders)
    assert not stale, "Remove cleaned files from _BASELINE: %s" % stale
