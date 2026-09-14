from __future__ import annotations

import hashlib

import pytest

from mac.persistence_redaction import (
    REDACTION_MARKER,
    redact_for_persistence,
)


def assert_secret_absent(secret: str, value: object, *, path: str) -> None:
    if secret in str(value):
        pytest.fail(f"credential fixture persisted at {path}", pytrace=False)


def credential_fixture(label: str) -> str:
    return hashlib.sha256(f"test-only:{label}".encode()).hexdigest()


def test_redact_for_persistence_preserves_shape_and_redacts_secret_fields():
    raw = {
        "verification": {
            "result": "ok\nMAC_ATTESTATION_KEY=attestation-value\n",
            "nested": [{"password": "database-value"}, {"count": 2}],
        }
    }

    assert redact_for_persistence(raw) == {
        "verification": {
            "result": f"ok\nMAC_ATTESTATION_KEY={REDACTION_MARKER}\n",
            "nested": [{"password": REDACTION_MARKER}, {"count": 2}],
        }
    }


def test_redact_for_persistence_keeps_nonsecret_token_prose():
    raw = {"summary": "Token accounting processed 42 input tokens."}
    assert redact_for_persistence(raw) == raw


@pytest.mark.parametrize(
    "template",
    [
        "executor failed with CURSOR_AUTH_TOKEN={secret}",
        'executor failed with CURSOR_AUTH_TOKEN="{secret} value"',
        "executor failed with CURSOR_AUTH_TOKEN = {secret} value",
    ],
)
def test_redact_for_persistence_redacts_assignments_embedded_in_prose(template: str):
    secret = "opaque-credential-fixture"
    raw = {"summary": template.format(secret=secret)}

    redacted = redact_for_persistence(raw)

    assert_secret_absent(secret, redacted, path="$.summary")


def test_redact_for_persistence_preserves_prose_after_unquoted_assignment():
    secret = "opaque-credential-fixture"
    raw = f"executor failed with CURSOR_AUTH_TOKEN={secret} retrying build"

    redacted = redact_for_persistence(raw)

    assert_secret_absent(secret, redacted, path="$")
    assert redacted == f"executor failed with CURSOR_AUTH_TOKEN={REDACTION_MARKER} retrying build"


def test_redact_for_persistence_preserves_prose_after_quoted_assignment():
    secret = "opaque-credential-fixture"
    raw = f'executor failed with CURSOR_AUTH_TOKEN="{secret} value" retrying build'

    redacted = redact_for_persistence(raw)

    assert_secret_absent(secret, redacted, path="$")
    assert redacted == f"executor failed with CURSOR_AUTH_TOKEN={REDACTION_MARKER} retrying build"


@pytest.mark.parametrize("quote", ['"', "'"])
def test_redact_for_persistence_fails_closed_for_unterminated_quoted_assignment(
    quote: str,
):
    secret = credential_fixture("unterminated-quote")
    raw = f"executor failed with CURSOR_AUTH_TOKEN={quote}{secret} trailing text"

    redacted = redact_for_persistence(raw)

    assert_secret_absent(secret, redacted, path="$")
    assert redacted == f"executor failed with CURSOR_AUTH_TOKEN={REDACTION_MARKER}"


def test_redact_for_persistence_redacts_backslash_escaped_whitespace_value():
    secret_head = credential_fixture("escaped-head")
    secret_tail = credential_fixture("escaped-tail")
    raw = f"executor failed with CURSOR_AUTH_TOKEN={secret_head}\\ {secret_tail} retrying build"

    redacted = redact_for_persistence(raw)

    assert_secret_absent(secret_head, redacted, path="$")
    assert_secret_absent(secret_tail, redacted, path="$")
    assert redacted == f"executor failed with CURSOR_AUTH_TOKEN={REDACTION_MARKER} retrying build"


@pytest.mark.parametrize("operator", ["&&", "||", "|"])
def test_redact_for_persistence_preserves_shell_operator_after_assignment(operator: str):
    secret = credential_fixture("shell-operator")
    raw = f"CURSOR_AUTH_TOKEN={secret}{operator} retrying build"

    redacted = redact_for_persistence(raw)

    assert_secret_absent(secret, redacted, path="$")
    assert redacted == f"CURSOR_AUTH_TOKEN={REDACTION_MARKER}{operator} retrying build"


def test_redact_for_persistence_redacts_camelcase_secret_keys():
    raw = {
        "apiKey": "camel-api-value",
        "refreshToken": "camel-refresh-value",
        "privateKey": "camel-private-value",
        "count": 3,
    }
    assert redact_for_persistence(raw) == {
        "apiKey": REDACTION_MARKER,
        "refreshToken": REDACTION_MARKER,
        "privateKey": REDACTION_MARKER,
        "count": 3,
    }


def test_redact_for_persistence_redacts_uppercase_and_embedded_secret_keys():
    raw = {
        "APIKey": "upper-api-value",
        "my_api_key_value": "embedded-api-value",
    }
    assert redact_for_persistence(raw) == {
        "APIKey": REDACTION_MARKER,
        "my_api_key_value": REDACTION_MARKER,
    }


def test_redact_for_persistence_redacts_authorization_header_remainder():
    secret = "header-secret-do-not-echo"
    raw = {"headers": f"Authorization: Bearer {secret} trailing-material"}
    redacted = redact_for_persistence(raw)
    assert secret not in str(redacted)
    assert "trailing-material" not in str(redacted)
    assert redacted["headers"] == f"Authorization: Bearer {REDACTION_MARKER}"


def test_redact_for_persistence_redacts_url_userinfo():
    secret = "url-user-do-not-echo"
    raw = {"endpoint": f"https://{secret}@example.com/path"}
    redacted = redact_for_persistence(raw)
    assert secret not in str(redacted)
    assert redacted["endpoint"] == f"https://{REDACTION_MARKER}@example.com/path"


def test_redact_for_persistence_redacts_known_token_formats():
    fixtures = {
        "openai": "sk-1234567890abcdef",
        "github": "ghp_1234567890abcdef",
        "slack": "xoxb-1234567890-ab",
    }
    for label, token in fixtures.items():
        raw = {"provider": {label: token}}
        redacted = redact_for_persistence(raw)
        assert token not in str(redacted)
        assert redacted["provider"][label] == REDACTION_MARKER


def test_redact_for_persistence_redacts_pem_private_key_under_neutral_key():
    secret = "PEM-BODY-DO-NOT-ECHO"
    pem = f"-----BEGIN PRIVATE KEY-----\n{secret}\n-----END PRIVATE KEY-----"
    raw = {"output": pem}
    redacted = redact_for_persistence(raw)
    assert secret not in str(redacted)
    assert "BEGIN PRIVATE KEY" not in str(redacted)
    assert redacted["output"] == REDACTION_MARKER


def test_redact_for_persistence_preserves_nonsecret_key_names():
    raw = {
        "tokenization": "morphology",
        "summary": "Token accounting processed 42 input tokens.",
        "note": "Authorization policy updated without credential rotation.",
        "url": "https://example.com/docs/api-key-management",
    }
    assert redact_for_persistence(raw) == raw


def test_redaction_preserves_provenance_counts_and_signature_identity():
    raw = {
        "credential_source": "fleet registry",
        "token_count": 42,
        "token_counts": {"input": 32, "output": 10},
        "signed_by": "agent_rocky",
        "signature": "a" * 64,
        "repo": {"head_sha": "b" * 40},
        "MAC_ATTESTATION_KEY": "opaque-test-credential",
        "signingKey": "opaque-test-credential",
    }
    expected = {**raw, "MAC_ATTESTATION_KEY": REDACTION_MARKER, "signingKey": REDACTION_MARKER}
    assert redact_for_persistence(raw) == expected
    assert redact_for_persistence(expected) == expected


def test_provenance_field_does_not_exempt_secret_shaped_value():
    raw = {"credential_source": "CURSOR_AUTH_TOKEN=opaque-test-credential"}
    assert redact_for_persistence(raw) == {
        "credential_source": f"CURSOR_AUTH_TOKEN={REDACTION_MARKER}"
    }


def test_embedded_json_and_truncated_private_key_are_redacted():
    secret = credential_fixture("json-and-truncated-key")
    for raw in [f'Failure: {{"token": "{secret}"}}', f"-----BEGIN PRIVATE KEY-----\n{secret}"]:
        assert_secret_absent(secret, redact_for_persistence(raw), path="output")


@pytest.mark.parametrize("name", ["TOKEN", "KEY", "PASSWORD", "SECRET", "token", "password"])
def test_bare_secret_assignment_is_redacted(name):
    raw = name + "=synthetic-test-credential"
    assert redact_for_persistence(raw) == name + "=" + REDACTION_MARKER
