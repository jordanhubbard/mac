
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
                description="Longer task brief: what, scope, success criteria. Sanitized — no private memory.",
            ),
            "required_capabilities": dict(
                _STRING_ARRAY,
                description="Capability slugs an executor must have to claim this task.",
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
            "project": dict(_OPTIONAL_STRING, description="Project slug, if known."),
            "snippets": dict(
                _STRING_ARRAY,
                description="Short verbatim quotes from the user that scope the task. Avoid private memory.",
            ),
            "links": dict(_STRING_ARRAY, description="Reference URIs (PR links, docs, etc.)."),
        },
        "required": ["title", "summary"],
    },
    "mac_list_tasks": {
        "type": "object",
        "properties": {
            "state": dict(_TASK_STATE, description="Filter by task state."),
            "tenant_id": dict(_OPTIONAL_STRING, description="Scope to one tenant."),
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
}


def schema_for(tool: ToolSpec) -> dict:
    return _SCHEMAS[tool.name]
