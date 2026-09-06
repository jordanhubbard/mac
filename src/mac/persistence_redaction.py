from __future__ import annotations

import re
from typing import Any, Mapping

REDACTION_MARKER = "<redacted>"

_SECRET_KEY_TERMS = (
    "api_key",
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
    r"(?im)(\b(?:export[ \t]+)?[A-Za-z_][A-Za-z0-9_]*"
    r"(?:KEY|PASSWORD|SECRET|TOKEN)[A-Za-z0-9_]*[ \t]*=[ \t]*)"
    r"(?:\"[^\r\n\"]*\"|'[^\r\n']*'|\"[^\r\n\"]*$|'[^\r\n']*$|"
    r"(?:\\[^\r\n]|[^\s;&|\"'])+)"
)
_AUTHORIZATION_RE = re.compile(r"(?im)(\bauthorization\s*:\s*(?:bearer\s+)?)[^\r\n]*")
_URL_USERINFO_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+)@")
_KNOWN_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,})\b"
)
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


class PersistenceSecretError(ValueError):
    """Raised when credential-shaped content reaches a fail-closed boundary."""


def _normalize_key_name(key: object) -> str:
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", str(key))
    return text.lower().replace("-", "_")


def _secret_key(key: object) -> bool:
    normalized = _normalize_key_name(key)
    compact = normalized.replace("_", "")
    for term in _SECRET_KEY_TERMS:
        if normalized == term or compact == term.replace("_", ""):
            return True
        if re.search(rf"(?:^|_){re.escape(term)}(?:$|_)", normalized):
            return True
    return False


def _apply_secret_redactions(text: str) -> str:
    value = _PEM_PRIVATE_KEY_RE.sub(REDACTION_MARKER, text)
    value = _URL_USERINFO_RE.sub(r"\1%s@" % REDACTION_MARKER, value)
    value = _AUTHORIZATION_RE.sub(r"\1%s" % REDACTION_MARKER, value)
    value = _ASSIGNMENT_RE.sub(r"\1%s" % REDACTION_MARKER, value)
    return _KNOWN_TOKEN_RE.sub(REDACTION_MARKER, value)


def _redact_text(text: str) -> str:
    return _apply_secret_redactions(text.replace("\x00", ""))


def _text_has_secrets(text: str) -> bool:
    normalized = text.replace("\x00", "")
    return _apply_secret_redactions(normalized) != normalized


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


def secret_paths(value: Any, *, _path: str = "$") -> list[str]:
    if isinstance(value, Mapping):
        paths: list[str] = []
        for key, item in value.items():
            child_path = f"{_path}.{key}"
            if _secret_key(key):
                paths.append(child_path)
            else:
                paths.extend(secret_paths(item, _path=child_path))
        return paths
    if isinstance(value, list):
        paths = []
        for index, item in enumerate(value):
            paths.extend(secret_paths(item, _path=f"{_path}[{index}]"))
        return paths
    if isinstance(value, tuple):
        paths = []
        for index, item in enumerate(value):
            paths.extend(secret_paths(item, _path=f"{_path}[{index}]"))
        return paths
    if isinstance(value, str):
        if _text_has_secrets(value):
            return [_path]
        return []
    return []


def assert_persistence_safe(value: Any, *, label: str) -> None:
    paths = secret_paths(value)
    if paths:
        raise PersistenceSecretError(
            f"{label} contains credential-shaped content at {', '.join(paths[:8])}"
        )
