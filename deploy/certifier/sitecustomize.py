"""Insert only the controller-selected candidate source after safe startup."""

from __future__ import annotations

import os
import sys
from pathlib import Path


raw_candidate = os.environ.get("MAC_CERTIFIER_CANDIDATE_SRC", "")
if raw_candidate:
    candidate = Path(raw_candidate)
    if not candidate.is_absolute():
        raise RuntimeError("MAC_CERTIFIER_CANDIDATE_SRC must be absolute")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_dir() or resolved.name != "src":
        raise RuntimeError("MAC_CERTIFIER_CANDIDATE_SRC is not a source directory")
    sys.path.insert(0, str(resolved))
