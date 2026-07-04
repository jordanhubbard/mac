
from __future__ import annotations

from .manifest import ToolSpec

_STRING = {"type": "string"}
_NON_EMPTY_STRING = {"type": "string", "minLength": 1}
_OPTIONAL_STRING = {"type": "string"}
_INTEGER = {"type": "integer"}
_BOOLEAN = {"type": "boolean"}
_STRING_ARRAY = {"type": "array", "items": _STRING}
_TASK_STATE = {
    "type": "string",
    "enum": [
        "open",
        "blocked",
        "claimed",
        "running",
        "needs_review",
        "reviewing",
        "completed",
        "failed",
        "cancelled",
    ],
}

_SCHEMAS: dict[str, dict] = {
    "mac_create_task": {
        "type": "object",
        "properties": {
            "title": dict(_NON_EMPTY_STRING, description="Short imperative summary of the work."),
            "summary": dict(
                _NON_EMPTY_STRING,
                description=(
                    "Longer task brief: target system/repository, what to change, "
                    "scope, acceptance criteria, and sanitized evidence. No private memory."
                ),
            ),
            "required_capabilities": dict(
                _STRING_ARRAY,
                description=(
                    "Hard runtime capability slugs only. Do not send language/domain "
                    "labels like 'typescript', 'frontend', or 'design' for normal "
                    "project work; send `project` and let MAC apply task_defaults.role. "
                    "Use this only for runner requirements such as ['ops'] for "
                    "Kubernetes/deploy/infra access."
                ),
            ),
            "priority": dict(_INTEGER, description="Higher = sooner. Default 0."),
            "platform_binding_id": dict(
                _OPTIONAL_STRING,
                description="Optional binding id tying this task to a Telegram/Slack thread.",
            ),
            "conversation_ref": dict(
                _OPTIONAL_STRING,
                description="Optional opaque URI referencing the user thread (e.g. telegram://chat/msg).",
            ),
            "project": dict(
                _OPTIONAL_STRING,
                description=(
                    "Project slug for executor routing. For MAC control-plane/API/UI/runner/plugin "
                    "work use 'mac'. MAC applies project task_defaults.role; do not add "
                    "language/domain capabilities for normal project work."
                ),
            ),
            "snippets": dict(
                _STRING_ARRAY,
                description="Short verbatim quotes from the user that scope the task. Avoid private memory.",
            ),
            "links": dict(_STRING_ARRAY, description="Reference URIs (PR links, docs, etc.)."),
            "tags": dict(
                _STRING_ARRAY,
                description=(
                    "Domain/language classification labels for the work, e.g. "
                    "['typescript','frontend','ui']. Put language/domain hints here, "
                    "not in required_capabilities. MAC stores them as task context."
                ),
            ),
        },
        "required": ["title", "summary"],
    },
    "mac_list_tasks": {
        "type": "object",
        "properties": {
            "state": dict(_TASK_STATE, description="Filter by task state."),
            "tenant_id": dict(_OPTIONAL_STRING, description="Scope to one tenant."),
            "project": dict(_OPTIONAL_STRING, description="Scope to one project (server-side filter)."),
            "limit": dict(_INTEGER, description="Cap the number of returned tasks (default: no limit)."),
        },
    },
    "mac_get_task": {
        "type": "object",
        "properties": {"task_id": dict(_NON_EMPTY_STRING, description="The mac task id.")},
        "required": ["task_id"],
    },
    "mac_task_summary": {
        "type": "object",
        "properties": {"task_id": dict(_NON_EMPTY_STRING, description="The mac task id.")},
        "required": ["task_id"],
    },
    "mac_cancel_task": {
        "type": "object",
        "properties": {
            "task_id": dict(_NON_EMPTY_STRING, description="The mac task id to cancel."),
            "actor": dict(_NON_EMPTY_STRING, description="The bound agent id performing the cancel."),
            "detail": {
                "type": "object",
                "properties": {
                    "reason": dict(
                        _NON_EMPTY_STRING, description="Short human-readable reason for cancelling."
                    )
                },
                "required": ["reason"],
            },
        },
        "required": ["task_id", "actor", "detail"],
    },
    "mac_work_brief": {
        "type": "object",
        "properties": {
            "include_completed": dict(_BOOLEAN, description="Include completed tasks? Default true."),
            "task_limit": dict(_INTEGER, description="Cap the task list. Default 100."),
        },
    },
    "mac_pending_notifications": {
        "type": "object",
        "properties": {
            "limit": dict(_INTEGER, description="Cap the notification list. Default 100."),
        },
    },
    "mac_ack_notification": {
        "type": "object",
        "properties": {
            "notification_id": dict(
                _NON_EMPTY_STRING, description="The mac notification id to mark delivered."
            ),
        },
        "required": ["notification_id"],
    },
}


def schema_for(tool: ToolSpec) -> dict:
    return _SCHEMAS[tool.name]
