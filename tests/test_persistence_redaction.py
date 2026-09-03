from __future__ import annotations

import pytest

from mac.persistence_redaction import (
    REDACTION_MARKER,
    PersistenceSecretError,
    assert_persistence_safe,
    redact_for_persistence,
    secret_paths,
)


def assert_secret_absent(secret: str, value: object, *, path: str) -> None:
    if secret in str(value):
        pytest.fail(f"credential fixture persisted at {path}", pytrace=False)


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
    assert secret_paths(raw) == ["$.summary"]


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


def test_secret_paths_reports_paths_without_values():
    secret = "unique-do-not-echo"
    value = {"verification": {"result": f"CURSOR_AUTH_TOKEN={secret}"}}
    assert secret_paths(value) == ["$.verification.result"]

    with pytest.raises(PersistenceSecretError) as caught:
        assert_persistence_safe(value, label="evidence")
    assert secret not in str(caught.value)
    assert "$.verification.result" in str(caught.value)


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
    assert secret_paths(raw) == ["$.apiKey", "$.refreshToken", "$.privateKey"]


def test_redact_for_persistence_redacts_uppercase_and_embedded_secret_keys():
    raw = {
        "APIKey": "upper-api-value",
        "my_api_key_value": "embedded-api-value",
    }
    assert redact_for_persistence(raw) == {
        "APIKey": REDACTION_MARKER,
        "my_api_key_value": REDACTION_MARKER,
    }
    assert secret_paths(raw) == ["$.APIKey", "$.my_api_key_value"]


def test_redact_for_persistence_redacts_authorization_header_remainder():
    secret = "header-secret-do-not-echo"
    raw = {"headers": f"Authorization: Bearer {secret} trailing-material"}
    redacted = redact_for_persistence(raw)
    assert secret not in str(redacted)
    assert "trailing-material" not in str(redacted)
    assert redacted["headers"] == f"Authorization: Bearer {REDACTION_MARKER}"
    assert secret_paths(raw) == ["$.headers"]


def test_redact_for_persistence_redacts_url_userinfo():
    secret = "url-user-do-not-echo"
    raw = {"endpoint": f"https://{secret}@example.com/path"}
    redacted = redact_for_persistence(raw)
    assert secret not in str(redacted)
    assert redacted["endpoint"] == f"https://{REDACTION_MARKER}@example.com/path"
    assert secret_paths(raw) == ["$.endpoint"]


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
        assert secret_paths(raw) == [f"$.provider.{label}"]


def test_redact_for_persistence_redacts_pem_private_key_under_neutral_key():
    secret = "PEM-BODY-DO-NOT-ECHO"
    pem = f"-----BEGIN PRIVATE KEY-----\n{secret}\n-----END PRIVATE KEY-----"
    raw = {"output": pem}
    redacted = redact_for_persistence(raw)
    assert secret not in str(redacted)
    assert "BEGIN PRIVATE KEY" not in str(redacted)
    assert redacted["output"] == REDACTION_MARKER
    assert secret_paths(raw) == ["$.output"]


def test_secret_paths_ignores_nul_only_strings():
    raw = {"payload": "safe-prefix\x00safe-suffix"}
    assert secret_paths(raw) == []
    assert redact_for_persistence(raw) == {"payload": "safe-prefixsafe-suffix"}


def test_redact_for_persistence_preserves_nonsecret_key_names():
    raw = {
        "tokenization": "morphology",
        "summary": "Token accounting processed 42 input tokens.",
        "note": "Authorization policy updated without credential rotation.",
        "url": "https://example.com/docs/api-key-management",
    }
    assert redact_for_persistence(raw) == raw
    assert secret_paths(raw) == []
