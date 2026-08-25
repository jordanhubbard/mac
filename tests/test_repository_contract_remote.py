from __future__ import annotations

import json

import pytest

from mac.repository_contract import (
    canonical_git_remote_identity,
    resolve_repository_canonical_remote,
    resolve_task_repository_branch,
    validate_secret_free_git_remote,
)


def test_contract_remote_is_authoritative_over_drifted_legacy_source() -> None:
    resolved = resolve_repository_canonical_remote(
        {
            "id": "repo",
            "source": "git@example.invalid:obsolete/repository.git",
            "metadata": json.dumps(
                {
                    "repository_contract": {
                        "canonical_remote_url": ("ssh://git@example.invalid/current/repository.git")
                    }
                }
            ),
        }
    )

    assert resolved.url == "ssh://git@example.invalid/current/repository.git"
    assert resolved.identity == "example.invalid/current/repository"
    assert resolved.source_kind == "repository_contract"


def test_legacy_source_is_used_only_when_contract_key_is_absent() -> None:
    legacy = resolve_repository_canonical_remote(
        {
            "id": "repo",
            "source": "git@example.invalid:legacy/repository.git",
            "metadata": "{}",
        }
    )
    assert legacy.url == "git@example.invalid:legacy/repository.git"
    assert legacy.source_kind == "legacy_source"

    for metadata in (
        {"repository_contract": None},
        {"repository_contract": {}},
        {"repository_contract": {"canonical_remote_url": ""}},
    ):
        with pytest.raises(ValueError, match="contract"):
            resolve_repository_canonical_remote(
                {
                    "id": "repo",
                    "source": "git@example.invalid:must-not-fallback.git",
                    "metadata": metadata,
                }
            )


@pytest.mark.parametrize(
    "remote",
    [
        "https://token@example.invalid/org/repository.git",
        "ssh://git:password@example.invalid/org/repository.git",
        "ssh://git@example.invalid/org/repository.git?token=secret",
        "git@example.invalid:org/repository.git?access_token=secret",
        "ssh://git@example.invalid/org/repository.git#secret",
        "ssh://git@example.invalid/org/repository.git\n--upload-pack=evil",
    ],
)
def test_canonical_remote_rejects_embedded_secret_material(remote: str) -> None:
    with pytest.raises(ValueError):
        validate_secret_free_git_remote(remote)


def test_remote_identity_normalizes_transport_and_default_port() -> None:
    assert canonical_git_remote_identity(
        "git@example.invalid:Org/repository.git"
    ) == canonical_git_remote_identity("ssh://git@example.invalid:22/Org/repository")


def test_task_branch_uses_current_execution_contract_without_legacy_copy() -> None:
    task = {
        "metadata": {
            "execution_contract": {
                "type": "repository",
                "repository_contract": {"default_branch": "feature/one"},
            },
            "origin": {"default_branch": "obsolete-main"},
        }
    }

    assert (
        resolve_task_repository_branch(
            task,
            legacy_branch=task["metadata"]["origin"]["default_branch"],
            environment_branch="also-obsolete",
            default_branch="main",
        )
        == "feature/one"
    )


def test_task_branch_rejects_incomplete_or_contradictory_contracts() -> None:
    with pytest.raises(ValueError, match="has no canonical branch"):
        resolve_task_repository_branch(
            {
                "metadata": {
                    "execution_contract": {
                        "type": "repository",
                        "repository_contract": {
                            "canonical_remote_url": "git@example.invalid:org/repo.git"
                        },
                    }
                }
            },
            default_branch="main",
        )

    with pytest.raises(ValueError, match="contradicts authoritative branch"):
        resolve_task_repository_branch(
            {
                "metadata": {
                    "execution_contract": {
                        "type": "repository",
                        "repository_contract": {"default_branch": "feature/one"},
                    },
                    "origin": {"repository_contract": {"default_branch": "main"}},
                }
            }
        )


def test_contract_free_task_branch_retains_explicit_legacy_fallback() -> None:
    assert (
        resolve_task_repository_branch(
            {"metadata": {"origin": {"type": "direct_task"}}},
            legacy_branch="release/legacy",
            default_branch="main",
        )
        == "release/legacy"
    )
