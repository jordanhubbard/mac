"""Round-trip (de)serialization tests for the ACP wire layer."""

from __future__ import annotations

from mac.acp.protocol import (
    PROTOCOL_VERSION,
    InitializeParams,
    InitializeResult,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
    Method,
    SessionUpdateKind,
    StopReason,
    decode_message,
)


def test_protocol_version_is_pinned_integer():
    # The ACP protocolVersion is a single MAJOR integer (currently 1), not a
    # string -- a correction vs the original research notes.
    assert PROTOCOL_VERSION == 1
    assert isinstance(PROTOCOL_VERSION, int)


def test_initialize_request_round_trips():
    req = JSONRPCRequest(
        id=1,
        method=Method.INITIALIZE,
        params=InitializeParams().to_dict(),
    )
    wire = req.to_dict()
    assert wire["jsonrpc"] == "2.0"
    assert wire["method"] == "initialize"
    assert wire["params"]["protocolVersion"] == PROTOCOL_VERSION

    decoded = decode_message(wire)
    assert isinstance(decoded, JSONRPCRequest)
    assert decoded.id == 1
    assert decoded.method == "initialize"

    params = InitializeParams.from_dict(decoded.params)
    assert params.protocol_version == PROTOCOL_VERSION
    assert "fs" in params.client_capabilities.to_dict()


def test_initialize_result_round_trips():
    result = InitializeResult()
    response = JSONRPCResponse(id=1, result=result.to_dict())
    wire = response.to_dict()
    assert "result" in wire and "error" not in wire
    assert wire["result"]["protocolVersion"] == PROTOCOL_VERSION
    assert wire["result"]["authMethods"] == []

    decoded = decode_message(wire)
    assert isinstance(decoded, JSONRPCResponse)
    assert not decoded.is_error
    parsed = InitializeResult.from_dict(decoded.result)
    assert parsed.protocol_version == PROTOCOL_VERSION
    assert parsed.agent_capabilities.load_session is False


def test_error_response_round_trips():
    response = JSONRPCResponse(
        id=7,
        error=JSONRPCError(code=-32601, message="method not found", data={"m": "x"}),
    )
    wire = response.to_dict()
    assert "error" in wire and "result" not in wire
    assert wire["error"]["code"] == -32601

    decoded = decode_message(wire)
    assert isinstance(decoded, JSONRPCResponse)
    assert decoded.is_error
    assert decoded.error is not None
    assert decoded.error.code == -32601
    assert decoded.error.message == "method not found"
    assert decoded.error.data == {"m": "x"}


def test_session_update_notification_round_trips():
    notif = JSONRPCNotification(
        method=Method.SESSION_UPDATE,
        params={
            "sessionId": "sess_abc",
            "update": {
                "sessionUpdate": SessionUpdateKind.AGENT_MESSAGE_CHUNK,
                "content": {"type": "text", "text": "hello"},
            },
        },
    )
    wire = notif.to_dict()
    assert wire["method"] == "session/update"
    assert "id" not in wire  # notifications carry no id

    decoded = decode_message(wire)
    assert isinstance(decoded, JSONRPCNotification)
    assert decoded.method == "session/update"
    assert decoded.params["update"]["sessionUpdate"] == "agent_message_chunk"


def test_decode_distinguishes_request_notification_response():
    assert isinstance(
        decode_message({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        JSONRPCRequest,
    )
    assert isinstance(
        decode_message({"jsonrpc": "2.0", "method": "session/update"}),
        JSONRPCNotification,
    )
    assert isinstance(
        decode_message({"jsonrpc": "2.0", "id": 1, "result": {}}),
        JSONRPCResponse,
    )


def test_stop_reason_constants_present():
    assert StopReason.END_TURN == "end_turn"
    assert StopReason.CANCELLED == "cancelled"
    assert StopReason.MAX_TURN_REQUESTS == "max_turn_requests"
