"""mac Hermes plugin.

Exposes six tools the LLM can call to drive the mac task ledger. The
plugin is loaded by hermes-agent from /opt/data/plugins/mac/ (dropped
there by the install-mac-plugin init container in home-ops).

Entry point: register(ctx) — called once by Hermes at startup.
"""

from __future__ import annotations

import json
from typing import Any

from .client import (
    check_mac_available,
    get_client,
    hermes_instance_id,
)
from .manifest import REQUIRED_ENV, TOOLS, TOOLS_BY_NAME, ToolSpec
from .schemas import schema_for


def _error_json(code: str, message: str, status: int) -> str:
    return json.dumps(
        {
            "ok": False,
            "error": {
                "code": code,
                "message": message,
                "status": status,
                "request_id": "",
            },
        }
    )


def _ensure_args(args: Any) -> dict | None:
    """Reject the obvious malformed-arg cases the LLM occasionally produces."""
    if args is None:
        return {}
    if not isinstance(args, dict):
        return None
    return dict(args)


def _resolve_body_for(tool: ToolSpec, args: dict) -> dict:
    """Apply per-tool body normalisation before the HTTP call."""
    if tool.name == "mac_create_task":
        # hermes_instance_id is a path param — the plugin always
        # provides it from env so the LLM doesn't have to remember.
        args.setdefault("hermes_instance_id", hermes_instance_id())
    elif tool.name == "mac_work_brief":
        args.setdefault("hermes_instance_id", hermes_instance_id())
    elif tool.name == "mac_cancel_task":
        # The cancel tool is a thin wrapper: the LLM provides
        # task_id/actor/detail; we fill the transition target.
        args["target_state"] = "cancelled"
    return args


async def _handle_tool(tool: ToolSpec, args: Any, **_: object) -> str:
    body = _ensure_args(args)
    if body is None:
        return _error_json("request.invalid_json", "Arguments must be an object", 400)

    try:
        body = _resolve_body_for(tool, body)
    except ValueError as exc:
        return _error_json("request.invalid_json", str(exc), 400)

    try:
        return await get_client().request(
            tool.method,
            tool.path,
            body,
            timeout=tool.timeout,
            retry=tool.retry,
        )
    except Exception as exc:  # noqa: BLE001 — Hermes tool boundary
        return _error_json(
            "service.internal_error",
            f"mac plugin failed: {exc}",
            500,
        )


def _make_handler(tool: ToolSpec):
    async def handler(args: dict, **kwargs: object) -> str:
        return await _handle_tool(tool, args, **kwargs)

    return handler


def register(ctx) -> None:
    """Hermes plugin entry point.

    ``ctx`` is the Hermes plugin context (provided by hermes-agent at
    plugin-load time). It supports ``register_tool(name, toolset,
    schema, handler, check_fn, requires_env, is_async, description)``.
    """
    for tool in TOOLS:
        ctx.register_tool(
            name=tool.name,
            toolset=tool.toolset,
            schema=schema_for(tool),
            handler=_make_handler(tool),
            check_fn=check_mac_available,
            requires_env=REQUIRED_ENV,
            is_async=True,
            description=tool.description,
        )


__all__ = [
    "register",
    "TOOLS",
    "TOOLS_BY_NAME",
    "REQUIRED_ENV",
    "schema_for",
    "check_mac_available",
    "get_client",
    "hermes_instance_id",
]
