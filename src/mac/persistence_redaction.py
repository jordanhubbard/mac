from __future__ import annotations

import json
import re
from typing import Any, Mapping

REDACTION_MARKER = "<redacted>"

_SECRET_KEY_TERMS = (
    "api_key",
    "attestation_key",
    "signing_key",
    "authorization",
    "bearer",
    "credential",
    "password",
    "private_key",
    "refresh_token",
    "secret",
    "token",
)
_ASSIGNMENT_RE = re.compile(
    r"(?im)(\b(?:export[ \t]+)?(?=[A-Za-z_])[A-Za-z0-9_]*"
    r"(?:KEY|PASSWORD|SECRET|TOKEN)[A-Za-z0-9_]*[ \t]*=[ \t]*)"
    r"""(?:"(?:\\[^\r\n]|[^"\\\r\n])*"|'[^'\r\n]*'|"""
    r""""(?:\\[^\r\n]|[^"\\\r\n])*(?:\\)?$|'[^'\r\n]*$|"""
    r"""(?:\\[^\r\n]|[^\s;&|"'\\])+)+"""
)
_AUTHORIZATION_RE = re.compile(r"(?im)(\bauthorization\s*:\s*(?:bearer\s+)?)[^\r\n]*")
_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,})\b"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)",
    re.DOTALL,
)


# These fields describe credential provenance or usage, not credential values.
# Their values are still recursively inspected for credential-shaped content.
_NONSECRET_FIELDS = frozenset(
    {
        "credential_source",
        "credential_source_name",
        "credential_reference",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "token_count",
        "token_counts",
        "prompt_tokens",
        "completion_tokens",
        "cached_tokens",
        "secret_redaction",
    }
)
_JSON_STRING_FIELD_RE = re.compile(r'("(?P<key>[^"\\]+)"\s*:\s*)("(?:\\.|[^"\\])*")')


def _normalize_key_name(key: object) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return text.lower().replace("-", "_")


def _secret_key(key: object) -> bool:
    normalized = _normalize_key_name(key)
    if normalized in _NONSECRET_FIELDS:
        return False
    compact = normalized.replace("_", "")
    for term in _SECRET_KEY_TERMS:
        if normalized == term or compact == term.replace("_", ""):
            return True
        if re.search(rf"(?:^|_){re.escape(term)}(?:$|_)", normalized):
            return True
    return False


def _apply_secret_redactions(text: str) -> str:
    value = _JSON_STRING_FIELD_RE.sub(
        lambda match: (
            match.group(1) + json.dumps(REDACTION_MARKER)
            if _secret_key(match.group("key"))
            else match.group(0)
        ),
        text,
    )
    value = _PEM_PRIVATE_KEY_RE.sub(REDACTION_MARKER, value)
    value = _URL_USERINFO_RE.sub(r"\1%s@" % REDACTION_MARKER, value)
    value = _AUTHORIZATION_RE.sub(r"\1%s" % REDACTION_MARKER, value)
    value = _ASSIGNMENT_RE.sub(r"\1%s" % REDACTION_MARKER, value)
    return _KNOWN_TOKEN_RE.sub(REDACTION_MARKER, value)


def _redact_text(text: str) -> str:
    return _apply_secret_redactions(text.replace("\x00", ""))


def redact_for_persistence(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): REDACTION_MARKER if _secret_key(key) else redact_for_persistence(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_for_persistence(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_for_persistence(item) for item in value)
    if isinstance(value, str):
        return _redact_text(value)
    return value
