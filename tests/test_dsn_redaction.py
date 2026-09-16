"""Credential-bearing DSNs must remain safe on passwordless test servers too."""

from urllib.parse import parse_qsl, urlsplit

import pytest

from mac.store_postgres import _redact_dsn


@pytest.mark.parametrize(
    "dsn",
    [
        "postgresql:///db?host=localhost&password=first-sensitive-value",
        "postgresql:///db?password=first-sensitive-value&password=second-sensitive-value",
        "postgresql:///db?%70assword=first-sensitive-value&sslpassword=second-sensitive-value",
        "postgresql://user:first-sensitive-value@[::1]:5432/db?password=second-sensitive-value",
    ],
)
def test_redacts_all_uri_password_locations(dsn):
    redacted = _redact_dsn(dsn)
    assert "sensitive-value" not in redacted
    assert "password" not in redacted
    assert urlsplit(redacted).path == "/db"


def test_redaction_preserves_nonsecret_connection_details():
    dsn = "postgresql://user:private-value@[::1]:5432/db?options=-c+search_path%3Dtest&password=hidden&sslmode=require"
    redacted = urlsplit(_redact_dsn(dsn))
    assert redacted.netloc == "user:***@[::1]:5432"
    assert dict(parse_qsl(redacted.query)) == {
        "options": "-c search_path=test",
        "sslmode": "require",
    }
