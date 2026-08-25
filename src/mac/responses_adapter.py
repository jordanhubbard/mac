"""Translate the OpenAI Responses wire contract onto a Chat Completions backend.

MAC's model router intentionally supports OpenAI-compatible upstreams, many of
which implement Chat Completions but not ``/v1/responses``.  Current Codex only
supports the Responses wire API.  This module keeps that client contract intact
while adapting messages, function calls, usage, and SSE lifecycle events at the
router boundary.

The streaming adapter buffers one upstream turn before emitting output events.
It still emits a standards-shaped Responses SSE lifecycle and preserves tool
calls, while avoiding an unsafe partial translation of arbitrarily fragmented
Chat Completion tool-call deltas.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Dict, Iterable, Iterator, List, Optional


def _new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex)


def _content_to_chat(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    parts: List[Dict[str, Any]] = []
    for raw in content:
        if isinstance(raw, str):
            parts.append({"type": "text", "text": raw})
            continue
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("type") or "")
        if kind in {"input_text", "output_text", "text"}:
            parts.append({"type": "text", "text": str(raw.get("text") or "")})
        elif kind in {"input_image", "image_url"} and raw.get("image_url"):
            image: Dict[str, Any] = {"url": raw.get("image_url")}
            if raw.get("detail"):
                image["detail"] = raw.get("detail")
            parts.append({"type": "image_url", "image_url": image})
    if not parts:
        return ""
    if all(part.get("type") == "text" for part in parts):
        return "".join(str(part.get("text") or "") for part in parts)
    return parts


def _responses_tools_to_chat(tools: Any) -> List[Dict[str, Any]]:
    converted: List[Dict[str, Any]] = []
    for raw in tools if isinstance(tools, list) else []:
        if not isinstance(raw, dict) or raw.get("type") != "function":
            continue
        function = {
            key: raw[key] for key in ("name", "description", "parameters", "strict") if key in raw
        }
        if function.get("name"):
            converted.append({"type": "function", "function": function})
    return converted


def responses_request_to_chat(body: Dict[str, Any]) -> Dict[str, Any]:
    """Convert one Responses request into an OpenAI Chat Completions request."""
    messages: List[Dict[str, Any]] = []
    instructions = body.get("instructions")
    if instructions:
        messages.append({"role": "system", "content": str(instructions)})

    inputs = body.get("input")
    if isinstance(inputs, str):
        messages.append({"role": "user", "content": inputs})
    elif isinstance(inputs, list):
        for item in inputs:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            kind = str(item.get("type") or "message")
            if kind == "message":
                role = str(item.get("role") or "user")
                if role == "developer":
                    role = "system"
                messages.append({"role": role, "content": _content_to_chat(item.get("content"))})
            elif kind == "function_call":
                call_id = str(item.get("call_id") or item.get("id") or _new_id("call"))
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": str(item.get("name") or ""),
                                    "arguments": str(item.get("arguments") or "{}"),
                                },
                            }
                        ],
                    }
                )
            elif kind == "function_call_output":
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(item.get("call_id") or item.get("id") or ""),
                        "content": _content_to_chat(item.get("output")),
                    }
                )

    out: Dict[str, Any] = {
        "model": body.get("model") or "*",
        "messages": messages or [{"role": "user", "content": ""}],
        "stream": bool(body.get("stream")),
    }
    tools = _responses_tools_to_chat(body.get("tools"))
    if tools:
        out["tools"] = tools
    tool_choice = body.get("tool_choice")
    if isinstance(tool_choice, dict) and tool_choice.get("type") == "function":
        out["tool_choice"] = {
            "type": "function",
            "function": {"name": tool_choice.get("name")},
        }
    elif tool_choice in {"auto", "none", "required"}:
        out["tool_choice"] = tool_choice
    if "parallel_tool_calls" in body:
        out["parallel_tool_calls"] = bool(body.get("parallel_tool_calls"))
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body.get("max_output_tokens")
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = reasoning.get("effort")
    if out["stream"]:
        out["stream_options"] = {"include_usage": True}
    return out


def _usage_to_responses(usage: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(usage, dict):
        return None
    input_tokens = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    output_tokens = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cached_tokens": int(
                (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
            )
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {
            "reasoning_tokens": int(
                (usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0
            )
        },
        "total_tokens": int(usage.get("total_tokens") or input_tokens + output_tokens),
    }


def _chat_message_to_output(message: Any) -> List[Dict[str, Any]]:
    message = message if isinstance(message, dict) else {}
    output: List[Dict[str, Any]] = []
    content = message.get("content")
    if isinstance(content, list):
        text = "".join(
            str(part.get("text") or "")
            for part in content
            if isinstance(part, dict) and part.get("type") in {"text", "output_text"}
        )
    else:
        text = str(content or "")
    if text:
        output.append(
            {
                "id": _new_id("msg"),
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        )
    for raw in message.get("tool_calls") or []:
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
        output.append(
            {
                "id": _new_id("fc"),
                "type": "function_call",
                "status": "completed",
                "call_id": str(raw.get("id") or _new_id("call")),
                "name": str(function.get("name") or ""),
                "arguments": str(function.get("arguments") or "{}"),
            }
        )
    return output


def _response_envelope(
    request: Dict[str, Any],
    *,
    output: List[Dict[str, Any]],
    status: str,
    usage: Optional[Dict[str, Any]],
    response_id: Optional[str] = None,
    created_at: Optional[int] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        "id": response_id or _new_id("resp"),
        "object": "response",
        "created_at": created_at or int(time.time()),
        "status": status,
        "error": None,
        "incomplete_details": None,
        "instructions": request.get("instructions"),
        "max_output_tokens": request.get("max_output_tokens"),
        "model": model or request.get("model") or "*",
        "output": output,
        "parallel_tool_calls": bool(request.get("parallel_tool_calls", True)),
        "previous_response_id": request.get("previous_response_id"),
        "reasoning": request.get("reasoning") or {"effort": None, "summary": None},
        "store": bool(request.get("store", False)),
        "temperature": request.get("temperature"),
        "text": request.get("text") or {"format": {"type": "text"}},
        "tool_choice": request.get("tool_choice") or "auto",
        "tools": request.get("tools") or [],
        "top_p": request.get("top_p"),
        "truncation": request.get("truncation") or "disabled",
        "usage": usage,
        "metadata": request.get("metadata") or {},
    }


def chat_response_to_responses(body: Dict[str, Any], request: Dict[str, Any]) -> Dict[str, Any]:
    choices = body.get("choices") if isinstance(body, dict) else []
    first = choices[0] if isinstance(choices, list) and choices else {}
    message = first.get("message") if isinstance(first, dict) else {}
    return _response_envelope(
        request,
        output=_chat_message_to_output(message),
        status="completed",
        usage=_usage_to_responses(body.get("usage")),
        model=str(body.get("model") or request.get("model") or "*"),
    )


def _chat_stream_result(chunks: Iterable[bytes]) -> Dict[str, Any]:
    buffer = ""
    text_parts: List[str] = []
    tool_calls: Dict[int, Dict[str, Any]] = {}
    usage: Any = None
    model = ""
    for chunk in chunks:
        buffer += bytes(chunk).decode("utf-8", "replace")
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            data = "\n".join(
                line[5:].lstrip() for line in frame.splitlines() if line.startswith("data:")
            )
            if not data or data == "[DONE]":
                continue
            try:
                event = json.loads(data)
            except (TypeError, ValueError):
                continue
            if not isinstance(event, dict):
                continue
            model = str(event.get("model") or model)
            if isinstance(event.get("usage"), dict):
                usage = event.get("usage")
            choices = event.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            delta = choices[0].get("delta") if isinstance(choices[0], dict) else {}
            if not isinstance(delta, dict):
                continue
            if isinstance(delta.get("content"), str):
                text_parts.append(delta["content"])
            for raw in delta.get("tool_calls") or []:
                if not isinstance(raw, dict):
                    continue
                index = int(raw.get("index") or 0)
                current = tool_calls.setdefault(
                    index,
                    {"id": "", "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if raw.get("id"):
                    current["id"] = str(raw["id"])
                function = raw.get("function")
                if isinstance(function, dict):
                    current["function"]["name"] += str(function.get("name") or "")
                    current["function"]["arguments"] += str(function.get("arguments") or "")
    return {
        "model": model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts) or None,
                    "tool_calls": [tool_calls[key] for key in sorted(tool_calls)],
                }
            }
        ],
        "usage": usage,
    }


def _sse(event: Dict[str, Any]) -> bytes:
    kind = str(event.get("type") or "message")
    return ("event: %s\ndata: %s\n\n" % (kind, json.dumps(event, separators=(",", ":")))).encode(
        "utf-8"
    )


def _response_events(response: Dict[str, Any]) -> Iterator[bytes]:
    pending = dict(response)
    pending["status"] = "in_progress"
    pending["output"] = []
    pending["usage"] = None
    yield _sse({"type": "response.created", "response": pending})
    yield _sse({"type": "response.in_progress", "response": pending})
    for output_index, item in enumerate(response.get("output") or []):
        in_progress = {**item, "status": "in_progress"}
        if item.get("type") == "message":
            in_progress["content"] = []
        elif item.get("type") == "function_call":
            in_progress["arguments"] = ""
        yield _sse(
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": in_progress,
            }
        )
        if item.get("type") == "message":
            part = (item.get("content") or [{}])[0]
            text = str(part.get("text") or "")
            empty_part = {"type": "output_text", "text": "", "annotations": []}
            yield _sse(
                {
                    "type": "response.content_part.added",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": 0,
                    "part": empty_part,
                }
            )
            if text:
                yield _sse(
                    {
                        "type": "response.output_text.delta",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "content_index": 0,
                        "delta": text,
                    }
                )
            yield _sse(
                {
                    "type": "response.output_text.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": 0,
                    "text": text,
                }
            )
            yield _sse(
                {
                    "type": "response.content_part.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "content_index": 0,
                    "part": part,
                }
            )
        elif item.get("type") == "function_call":
            arguments = str(item.get("arguments") or "{}")
            if arguments:
                yield _sse(
                    {
                        "type": "response.function_call_arguments.delta",
                        "item_id": item["id"],
                        "output_index": output_index,
                        "delta": arguments,
                    }
                )
            yield _sse(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "arguments": arguments,
                }
            )
        yield _sse(
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": item,
            }
        )
    yield _sse({"type": "response.completed", "response": response})


def chat_stream_to_responses(chunks: Iterable[bytes], request: Dict[str, Any]) -> Iterator[bytes]:
    """Convert a Chat Completions SSE iterator into Responses SSE events."""
    chat = _chat_stream_result(chunks)
    yield from _response_events(chat_response_to_responses(chat, request))


def buffered_chat_to_responses_stream(
    body: Dict[str, Any], request: Dict[str, Any]
) -> Iterator[bytes]:
    """Emit Responses SSE for a buffered Chat Completions response."""
    yield from _response_events(chat_response_to_responses(body, request))
