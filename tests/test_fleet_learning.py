from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from mac.fleet_learning import (
    build_repository_access_learning,
    build_repository_access_memory_payload,
    classify_repository_access_failure,
    repository_access_state,
    repository_host,
    resolve_git_remote_access,
    task_repository_remote,
)


def test_resolve_git_remote_access_names_mechanism_without_token() -> None:
    access = resolve_git_remote_access(
        "https://github.com/acme/private.git",
        environ={"GH_TOKEN": "top-secret"},
    )

    assert access.host == "github.com"
    assert access.transport == "https"
    assert access.credential_source == "env:GH_TOKEN"
    assert "top-secret" not in repr(access)
    assert repository_host("git@github.com:acme/private.git") == "github.com"


def test_repository_access_learning_redacts_secrets_and_classifies_auth() -> None:
    error = (
        "fatal: unable to access "
        "https://x-access-token:top-secret@github.com/acme/private.git: "
        "could not read Username for 'https://github.com'"
    )
    learning = build_repository_access_learning(
        project="demo",
        remote="https://github.com/acme/private.git",
        operation="review_clone",
        agent_id="agent_a",
        outcome="failure",
        credential_source="env:GH_TOKEN",
        task_id="task_1",
        review_id="review_1",
        error=error,
    )
    payload = build_repository_access_memory_payload(learning)
    serialized = json.dumps(payload, sort_keys=True)

    assert learning["failure_class"] == "authentication"
    assert "top-secret" not in serialized
    assert "<redacted>" in serialized
    assert "prefer a peer with a recent successful access learning" in learning["recommendation"]
    assert classify_repository_access_failure(error) == "authentication"


def test_repository_access_state_uses_newest_result_and_expires_failures() -> None:
    now = datetime.now(timezone.utc)
    failure = build_repository_access_learning(
        project="demo",
        remote="https://github.com/acme/private.git",
        operation="review_clone",
        agent_id="agent_a",
        outcome="failure",
        credential_source="ambient:https",
        failure_class="authentication",
        at=(now - timedelta(seconds=20)).isoformat(),
    )
    records = [
        {
            "content": json.dumps(failure),
            "created_at": (now - timedelta(seconds=20)).isoformat(),
        }
    ]

    state, _ = repository_access_state(
        records,
        project="demo",
        host="github.com",
        operation="review_clone",
        failure_cooldown_seconds=60,
        success_ttl_seconds=3600,
        now=now,
    )
    assert state == "failure"

    success = build_repository_access_learning(
        project="demo",
        remote="https://github.com/acme/private.git",
        operation="review_clone",
        agent_id="agent_a",
        outcome="success",
        credential_source="env:GH_TOKEN",
        at=(now - timedelta(seconds=5)).isoformat(),
    )
    records.append(
        {
            "content": json.dumps(success),
            "created_at": (now - timedelta(seconds=5)).isoformat(),
        }
    )
    state, latest = repository_access_state(
        records,
        project="demo",
        host="github.com",
        operation="review_clone",
        failure_cooldown_seconds=60,
        success_ttl_seconds=3600,
        now=now,
    )
    assert state == "success"
    assert latest is not None and latest["credential_source"] == "env:GH_TOKEN"

    stale_state, _ = repository_access_state(
        records[:1],
        project="demo",
        host="github.com",
        operation="review_clone",
        failure_cooldown_seconds=10,
        success_ttl_seconds=3600,
        now=now,
    )
    assert stale_state == "unknown"


def test_task_repository_remote_prefers_contract_canonical_url() -> None:
    task = {
        "metadata": {
            "execution_contract": {
                "repository_contract": {"canonical_remote_url": "git@github.com:acme/canonical.git"}
            },
            "origin": {"repository_url": "https://github.com/acme/fallback.git"},
        }
    }

    assert task_repository_remote(task) == "git@github.com:acme/canonical.git"
