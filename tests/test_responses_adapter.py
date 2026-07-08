"""Behavioral coverage for the Responses-to-Chat router boundary."""

from __future__ import annotations

import json

from mac.responses_adapter import (
    chat_response_to_responses,
    chat_stream_to_responses,
    responses_request_to_chat,
)


def _events(chunks):
    events = []
    for chunk in chat_stream_to_responses(chunks, {"model": "gpt-test", "stream": True}):
        frame = chunk.decode("utf-8")
        data = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
        events.append(json.loads(data))
    return events


def test_request_maps_instructions_messages_tools_and_function_history():
    chat = responses_request_to_chat(
        {
            "model": "gpt-test",
            "instructions": "Work carefully",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "inspect"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "shell",
                    "arguments": '{"cmd":"pwd"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "/repo",
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "shell",
                    "description": "Run a command",
                    "parameters": {"type": "object"},
                    "strict": True,
                }
            ],
            "tool_choice": {"type": "function", "name": "shell"},
            "parallel_tool_calls": False,
            "max_output_tokens": 123,
            "reasoning": {"effort": "high"},
            "stream": True,
        }
    )

    assert chat["messages"] == [
        {"role": "system", "content": "Work carefully"},
        {"role": "user", "content": "inspect"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "shell", "arguments": '{"cmd":"pwd"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "/repo"},
    ]
    assert chat["tools"][0]["function"]["name"] == "shell"
    assert chat["tool_choice"] == {"type": "function", "function": {"name": "shell"}}
    assert chat["parallel_tool_calls"] is False
    assert chat["max_tokens"] == 123
    assert chat["reasoning_effort"] == "high"
    assert chat["stream_options"] == {"include_usage": True}


def test_nonstream_text_response_preserves_usage():
    response = chat_response_to_responses(
        {
            "model": "served-model",
            "choices": [{"message": {"role": "assistant", "content": "done"}}],
            "usage": {
                "prompt_tokens": 7,
                "completion_tokens": 3,
                "total_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 2},
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
        },
        {"model": "requested-model"},
    )

    assert response["object"] == "response"
    assert response["status"] == "completed"
    assert response["model"] == "served-model"
    assert response["output"][0]["content"][0]["text"] == "done"
    assert response["usage"] == {
        "input_tokens": 7,
        "input_tokens_details": {"cached_tokens": 2},
        "output_tokens": 3,
        "output_tokens_details": {"reasoning_tokens": 1},
        "total_tokens": 10,
    }


def test_nonstream_function_call_uses_responses_item_shape():
    response = chat_response_to_responses(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_9",
                                "type": "function",
                                "function": {"name": "shell", "arguments": '{"cmd":"ls"}'},
                            }
                        ],
                    }
                }
            ]
        },
        {"model": "gpt-test"},
    )

    item = response["output"][0]
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_9"
    assert item["name"] == "shell"
    assert item["arguments"] == '{"cmd":"ls"}'


def test_stream_maps_fragmented_text_to_responses_lifecycle():
    events = _events(
        [
            b'data: {"model":"served","choices":[{"delta":{"content":"hel"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"prompt_tokens":2,',
            b'"completion_tokens":1,"total_tokens":3}}\n\ndata: [DONE]\n\n',
        ]
    )

    assert [event["type"] for event in events] == [
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.completed",
    ]
    assert next(event for event in events if event["type"] == "response.output_text.delta")["delta"] == "hello"
    assert events[-1]["response"]["usage"]["total_tokens"] == 3


def test_stream_reassembles_fragmented_tool_call_deltas():
    events = _events(
        [
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_3",',
            b'"function":{"name":"she","arguments":"{\\\"cmd\\\":"}}]}}]}\n\n',
            b'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":',
            b'{"name":"ll","arguments":"\\\"pwd\\\"}"}}]}}]}\n\ndata: [DONE]\n\n',
        ]
    )

    item = next(event for event in events if event["type"] == "response.output_item.done")["item"]
    assert item["type"] == "function_call"
    assert item["call_id"] == "call_3"
    assert item["name"] == "shell"
    assert item["arguments"] == '{"cmd":"pwd"}'
    assert any(event["type"] == "response.function_call_arguments.done" for event in events)
