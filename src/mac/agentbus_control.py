from __future__ import annotations

from typing import Any, Dict, List, Optional

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

HERMES_CONFIG_APPLY_SCHEMA = "mac.agentbus.hermes_config_apply.v1"
HERMES_CONFIG_APPLY_TOPIC = "mac.hermes.config.apply.v1"
HERMES_CONFIG_APPLY_CONTENT_TYPE = "application/vnd.mac.hermes-config-apply+json"

HERMES_CONFIG_APPLY_RESULT_SCHEMA = "mac.agentbus.hermes_config_apply_result.v1"
HERMES_CONFIG_APPLY_RESULT_TOPIC = "mac.hermes.config.apply.result.v1"
HERMES_CONFIG_APPLY_RESULT_CONTENT_TYPE = "application/vnd.mac.hermes-config-apply-result+json"

DEBUG_TERMINAL_OPEN_SCHEMA = "mac.agentbus.debug_terminal_open.v1"
DEBUG_TERMINAL_OPEN_TOPIC = "mac.debug.terminal.open.v1"
DEBUG_TERMINAL_OPEN_CONTENT_TYPE = "application/vnd.mac.debug-terminal-open+json"

DEBUG_TERMINAL_INPUT_SCHEMA = "mac.agentbus.debug_terminal_input.v1"
DEBUG_TERMINAL_INPUT_TOPIC = "mac.debug.terminal.input.v1"
DEBUG_TERMINAL_INPUT_CONTENT_TYPE = "application/vnd.mac.debug-terminal-input+json"

DEBUG_TERMINAL_OUTPUT_SCHEMA = "mac.agentbus.debug_terminal_output.v1"
DEBUG_TERMINAL_OUTPUT_TOPIC = "mac.debug.terminal.output.v1"
DEBUG_TERMINAL_OUTPUT_CONTENT_TYPE = "application/vnd.mac.debug-terminal-output+json"

REFLECT_REQUEST_SCHEMA = "mac.agentbus.reflect_request.v1"
REFLECT_REQUEST_TOPIC = "mac.reflect.request.v1"
REFLECT_REQUEST_CONTENT_TYPE = "application/vnd.mac.reflect-request+json"

REFLECT_RESULT_SCHEMA = "mac.agentbus.reflect_result.v1"
REFLECT_RESULT_TOPIC = "mac.reflect.result.v1"
REFLECT_RESULT_CONTENT_TYPE = "application/vnd.mac.reflect-result+json"


def repo_update_payload(
    *,
    repo_path: Optional[str] = None,
    remote: str = "origin",
    branch: str = "main",
    restart: bool = True,
    restart_services: Optional[List[str]] = None,
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
    if restart_services:
        payload["restart_services"] = list(restart_services)
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


def hermes_config_apply_payload(
    *,
    payload: JsonDict,
    fleet_id: Optional[str] = None,
    fleet_name: Optional[str] = None,
    registry_path: Optional[str] = None,
    request_id: Optional[str] = None,
) -> JsonDict:
    message: JsonDict = {
        "schema": HERMES_CONFIG_APPLY_SCHEMA,
        "payload": payload,
    }
    if fleet_id:
        message["fleet_id"] = fleet_id
    if fleet_name:
        message["fleet_name"] = fleet_name
    if registry_path:
        message["registry_path"] = registry_path
    if request_id:
        message["request_id"] = request_id
    return message


def reflect_request_payload(
    *,
    sender_agent_id: str,
    query: str,
    request_id: Optional[str] = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema": REFLECT_REQUEST_SCHEMA,
        "sender_agent_id": sender_agent_id,
        "query": query,
    }
    if request_id:
        payload["request_id"] = request_id
    return payload


def reflect_result_payload(
    *,
    request_id: str,
    agent_id: str,
    response: str,
    word_count: int,
) -> JsonDict:
    return {
        "schema": REFLECT_RESULT_SCHEMA,
        "request_id": request_id,
        "agent_id": agent_id,
        "response": response,
        "word_count": int(word_count),
    }


def debug_terminal_open_payload(
    *,
    session_id: str,
    input_stream_id: str,
    output_stream_id: str,
    sender_agent_id: str,
    shell: Optional[str] = None,
    cwd: Optional[str] = None,
    rows: int = 32,
    cols: int = 120,
    ttl_seconds: int = 900,
    request_id: Optional[str] = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema": DEBUG_TERMINAL_OPEN_SCHEMA,
        "session_id": session_id,
        "input_stream_id": input_stream_id,
        "output_stream_id": output_stream_id,
        "sender_agent_id": sender_agent_id,
        "rows": int(rows),
        "cols": int(cols),
        "ttl_seconds": int(ttl_seconds),
    }
    if shell:
        payload["shell"] = shell
    if cwd:
        payload["cwd"] = cwd
    if request_id:
        payload["request_id"] = request_id
    return payload


def debug_terminal_input_payload(
    *,
    session_id: str,
    data_b64: Optional[str] = None,
    close: bool = False,
    rows: Optional[int] = None,
    cols: Optional[int] = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema": DEBUG_TERMINAL_INPUT_SCHEMA,
        "session_id": session_id,
    }
    if data_b64 is not None:
        payload["data_b64"] = data_b64
    if close:
        payload["close"] = True
    if rows is not None or cols is not None:
        payload["resize"] = {
            "rows": int(rows or 0),
            "cols": int(cols or 0),
        }
    return payload


def debug_terminal_output_payload(
    *,
    session_id: str,
    event: str,
    data_b64: Optional[str] = None,
    message: Optional[str] = None,
    exit_code: Optional[int] = None,
) -> JsonDict:
    payload: JsonDict = {
        "schema": DEBUG_TERMINAL_OUTPUT_SCHEMA,
        "session_id": session_id,
        "event": event,
    }
    if data_b64 is not None:
        payload["data_b64"] = data_b64
    if message:
        payload["message"] = message
    if exit_code is not None:
        payload["exit_code"] = int(exit_code)
    return payload
