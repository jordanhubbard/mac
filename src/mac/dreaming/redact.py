"""Privacy filtering for dream inputs and outputs.

Carried over near-verbatim from the previous ``dream_scanner`` /
``dream_repair_tasks`` implementation, which got this part right: six passes
covering URL userinfo, bearer tokens, known token shapes, secret assignments,
long opaque atoms, home paths, agent ids, and e-mail addresses.

The old cycle applied it and skipped every *quality* gate. Here it is one gate
among four (see :mod:`mac.dreaming.gates`) rather than the only one.
"""

from __future__ import annotations

import re
from typing import Any

_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_SECRET_ASSIGN_RE = re.compile(
    r"(?i)([\"']?(?:api[_-]?key|token|secret|password|authorization|"
    r"access[_-]?token|refresh[_-]?token)[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;}]+"
)
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,})\b"
)
_LONG_ATOM_RE = re.compile(r"\b[A-Za-z0-9_./+=-]{80,}\b")
_HOME_PATH_RE = re.compile(r"(?i)(/Users|/home)/[A-Za-z0-9._-]+")
_AGENT_ID_RE = re.compile(r"\bagent[_-][A-Za-z0-9_.-]+\b")
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_SPACE_RE = re.compile(r"\s+")

REDACTION_MARKER = "<redacted>"

#: Patterns that must not survive into a promoted memory. Used by the privacy
#: gate to verify the filter actually ran, rather than trusting that it did.
_LEAK_PATTERNS = (
    ("bearer_token", _BEARER_RE),
    ("known_token", _KNOWN_TOKEN_RE),
    ("secret_assignment", _SECRET_ASSIGN_RE),
    ("url_userinfo", _URL_USERINFO_RE),
    ("email", _EMAIL_RE),
    ("home_path", _HOME_PATH_RE),
)


def redact(value: Any, *, limit: int = 2000, collapse_space: bool = True) -> str:
    """Strip credentials and local identity from *value*."""

    text = str(value or "").replace("\x00", "")
    text = _URL_USERINFO_RE.sub(r"\1%s@" % REDACTION_MARKER, text)
    text = _BEARER_RE.sub(r"\1%s" % REDACTION_MARKER, text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: "%s%s" % (m.group(1), REDACTION_MARKER), text)
    text = _KNOWN_TOKEN_RE.sub(REDACTION_MARKER, text)
    text = _LONG_ATOM_RE.sub("<redacted-long-value>", text)
    text = _HOME_PATH_RE.sub(r"\1/<user>", text)
    text = _AGENT_ID_RE.sub("<agent>", text)
    text = _EMAIL_RE.sub("<email>", text)
    if collapse_space:
        text = _SPACE_RE.sub(" ", text.replace("\r", " ").replace("\n", " ")).strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)].rstrip() + "…"
    return text


def leaks(value: Any) -> list[str]:
    """Names of leak patterns still present in *value* after redaction.

    ``_HOME_PATH_RE`` and ``_EMAIL_RE`` are checked against their *post*
    substitution forms, so a correctly redacted string reports nothing.
    """

    text = str(value or "")
    found: list[str] = []
    for name, pattern in _LEAK_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        # A redacted hit still matches some patterns (e.g. "bearer <redacted>"),
        # so only count matches whose payload is not the marker itself.
        if REDACTION_MARKER in match.group(0) or "<user>" in match.group(0):
            continue
        if name == "email" and "<email>" in match.group(0):
            continue
        found.append(name)
    return sorted(set(found))


__all__ = ["REDACTION_MARKER", "leaks", "redact"]
