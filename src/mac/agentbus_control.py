from __future__ import annotations

from typing import Any, Dict, Optional

JsonDict = Dict[str, Any]

REPO_UPDATE_SCHEMA = "mac.agentbus.repo_update.v1"
REPO_UPDATE_TOPIC = "mac.repo.update.v1"
REPO_UPDATE_CONTENT_TYPE = "application/vnd.mac.repo-update+json"

REPO_UPDATE_RESULT_SCHEMA = "mac.agentbus.repo_update_result.v1"
REPO_UPDATE_RESULT_TOPIC = "mac.repo.update.result.v1"
REPO_UPDATE_RESULT_CONTENT_TYPE = "application/vnd.mac.repo-update-result+json"

ARTIFACT_PUBLISH_SCHEMA = "mac.agentbus.artifact_publish.v1"
ARTIFACT_PUBLISH_TOPIC = "mac.artifact.publish.v1"
ARTIFACT_PUBLISH_CONTENT_TYPE = "application/vnd.mac.artifact-publish+json"


def repo_update_payload(
    *,
    repo_path: Optional[str] = None,
    remote: str = "origin",
    branch: str = "main",
    restart: bool = True,
    request_id: Optional[str] = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema": REPO_UPDATE_SCHEMA,
        "remote": remote,
        "branch": branch,
        "restart": bool(restart),
    }
    if repo_path:
        payload["repo_path"] = repo_path
    if request_id:
        payload["request_id"] = request_id
    return payload


def artifact_publish_payload(
    *,
    operation: str,
    artifact: Optional[JsonDict] = None,
    artifacts: Optional[list[JsonDict]] = None,
    publish_dir: Optional[str] = None,
    public_url: Optional[str] = None,
    path: Optional[str] = None,
    request_id: Optional[str] = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema": ARTIFACT_PUBLISH_SCHEMA,
        "operation": operation,
    }
    if artifact is not None:
        payload["artifact"] = artifact
    if artifacts is not None:
        payload["artifacts"] = artifacts
    if publish_dir:
        payload["publish_dir"] = publish_dir
    if public_url:
        payload["public_url"] = public_url
    if path:
        payload["path"] = path
    if request_id:
        payload["request_id"] = request_id
    return payload
