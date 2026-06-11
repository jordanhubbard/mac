
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


def _shape_create_task_body(args: dict) -> dict:
    title = (args.get("title") or "").strip()
    summary = (args.get("summary") or "").strip()
    snippets = [s.strip() for s in (args.get("snippets") or []) if str(s).strip()]
    links = [l.strip() for l in (args.get("links") or []) if str(l).strip()]
    tags = [t.strip() for t in (args.get("tags") or []) if str(t).strip()]

    sections: list[str] = []
    if summary:
        sections.append(f"Summary:\n{summary}")
    if snippets:
        sections.append("Relevant excerpts:\n" + "\n".join(f"- {s}" for s in snippets))
    if links:
        sections.append("References:\n" + "\n".join(f"- {l}" for l in links))

    body: dict = {
        "title": title,
        "description": "\n\n".join(sections),
        "actor": "hermes",
    }
    for key in (
        "priority",
        "project",
        "required_capabilities",
        "dependencies",
        "metadata",
        "max_attempts",
        "platform_binding_id",
        "conversation_ref",
        "user_id",
    ):
        if args.get(key) is not None:
            body[key] = args[key]

    # Domain/language labels (e.g. "typescript", "frontend", "ui") are
    # classification hints, not hard runtime requirements. Carry them in
    # metadata.domain_capabilities so MAC routing stays role-driven.
    if tags:
        metadata = dict(body.get("metadata") or {})
        existing = metadata.get("domain_capabilities")
        domain = list(existing) if isinstance(existing, list) else []
        for tag in tags:
            if tag not in domain:
                domain.append(tag)
        metadata["domain_capabilities"] = domain
        body["metadata"] = metadata

    body["hermes_instance_id"] = args.get("hermes_instance_id") or hermes_instance_id()
    return body


def _resolve_body_for(tool: ToolSpec, args: dict) -> dict:
    """Apply per-tool body normalisation before the HTTP call."""
    if tool.name == "mac_create_task":
        return _shape_create_task_body(args)
    if tool.name == "mac_work_brief":
        args.setdefault("hermes_instance_id", hermes_instance_id())
    elif tool.name == "mac_cancel_task":
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
