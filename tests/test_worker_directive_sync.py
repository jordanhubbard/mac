from __future__ import annotations

import pytest

from mac.api_client import MacApiClient, MacApiError
from mac.worker import _synchronize_directive_policy


def test_worker_acknowledges_exact_pending_digest_then_confirms() -> None:
    calls = []
    reads = 0
    document = {"schema": "mac.directive.v1", "name": "test.rule"}
    import hashlib
    import json

    digest = hashlib.sha256(
        json.dumps(
            document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()

    def transport(method, path, payload):
        nonlocal reads
        calls.append((method, path, payload))
        if method == "GET":
            reads += 1
            return {
                "schema": "mac.directive.snapshot.v1",
                "enabled": True,
                "pending_activations": (
                    [
                        {
                            "activation_id": "activation_1",
                            "digest": digest,
                            "document": document,
                        }
                    ]
                    if reads == 1
                    else []
                ),
            }
        assert method == "POST"
        return {"state": "active"}

    snapshot = _synchronize_directive_policy(
        MacApiClient("http://hub", transport=transport), "agent_one"
    )

    assert snapshot["pending_activations"] == []
    assert calls == [
        ("GET", "/agents/agent_one/directives/effective", None),
        (
            "POST",
            "/agents/agent_one/directive-activations/activation_1/ack",
            {"digest": digest},
        ),
        ("GET", "/agents/agent_one/directives/effective", None),
    ]


def test_worker_fails_closed_when_ack_does_not_clear_pending_epoch() -> None:
    import hashlib
    import json

    document = {"schema": "mac.directive.v1", "name": "test.rule"}
    digest = hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    def transport(method, path, payload):
        if method == "POST":
            return {"state": "distributing"}
        return {
            "schema": "mac.directive.snapshot.v1",
            "enabled": True,
            "pending_activations": [
                {
                    "activation_id": "activation_1",
                    "digest": digest,
                    "document": document,
                }
            ],
        }

    with pytest.raises(MacApiError, match="did not clear"):
        _synchronize_directive_policy(
            MacApiClient("http://hub", transport=transport), "agent_one"
        )


def test_worker_tolerates_only_pre_directive_hub_not_found() -> None:
    def transport(_method, _path, _payload):
        raise MacApiError('{"detail":"Not Found"}')

    snapshot = _synchronize_directive_policy(
        MacApiClient("http://old-hub", transport=transport), "agent_one"
    )
    assert snapshot["enabled"] is False


def test_worker_propagates_current_hub_transport_failure() -> None:
    def transport(_method, _path, _payload):
        raise MacApiError("connection reset")

    with pytest.raises(MacApiError, match="connection reset"):
        _synchronize_directive_policy(
            MacApiClient("http://hub", transport=transport), "agent_one"
        )
