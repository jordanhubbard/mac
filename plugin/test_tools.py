"""Contract tests for the mac Hermes plugin tool surface."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from . import _resolve_body_for
from .client import _expand_path
from .manifest import TOOLS, TOOLS_BY_NAME
from .schemas import schema_for

_PLUGIN_DIR = Path(__file__).resolve().parent


# Guard: register() does an unguarded _SCHEMAS[tool.name] lookup, so a tool without a schema KeyErrors at load; fail fast here instead.
@pytest.mark.parametrize("tool", TOOLS, ids=lambda t: t.name)
def test_every_tool_has_a_schema(tool):
    schema = schema_for(tool)
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"


def test_plugin_yaml_lists_every_tool():
    """plugin.yaml provides_tools must stay in sync with TOOLS."""
    manifest = yaml.safe_load((_PLUGIN_DIR / "plugin.yaml").read_text())
    declared = set(manifest.get("provides_tools") or [])
    coded = {tool.name for tool in TOOLS}
    assert declared == coded, (
        "plugin.yaml drift: missing=%s extra=%s"
        % (sorted(coded - declared), sorted(declared - coded))
    )


# --- Expansion: mac_pending_notifications ------------------------------
def test_pending_notifications_expands_to_filtered_get():
    tool = TOOLS_BY_NAME["mac_pending_notifications"]
    assert tool.method == "GET"
    path, args = _expand_path(tool.path, dict(_resolve_body_for(tool, {})))
    assert path == "/notifications"
    assert args == {"status": "pending", "subject_type": "task"}


def test_pending_notifications_passes_limit_and_keeps_defaults():
    tool = TOOLS_BY_NAME["mac_pending_notifications"]
    path, args = _expand_path(tool.path, dict(_resolve_body_for(tool, {"limit": 50})))
    assert path == "/notifications"
    assert args == {"status": "pending", "subject_type": "task", "limit": 50}


def test_pending_notifications_allows_status_override():
    tool = TOOLS_BY_NAME["mac_pending_notifications"]
    # setdefault must not clobber a caller-supplied status.
    assert _resolve_body_for(tool, {"status": "delivered"})["status"] == "delivered"


# --- Expansion: mac_ack_notification -----------------------------------
def test_ack_notification_consumes_id_into_path():
    tool = TOOLS_BY_NAME["mac_ack_notification"]
    assert tool.method == "POST"
    body = _resolve_body_for(tool, {"notification_id": "ntf_123"})
    path, args = _expand_path(tool.path, dict(body))
    assert path == "/notifications/ntf_123/delivered"
    # notification_id consumed by the path; explicit delivered status remains.
    assert args == {"status": "delivered"}


def test_ack_notification_url_encodes_id():
    tool = TOOLS_BY_NAME["mac_ack_notification"]
    body = _resolve_body_for(tool, {"notification_id": "a/b c"})
    path, _ = _expand_path(tool.path, dict(body))
    assert path == "/notifications/a%2Fb%20c/delivered"


def test_ack_notification_requires_id():
    tool = TOOLS_BY_NAME["mac_ack_notification"]
    body = _resolve_body_for(tool, {})
    with pytest.raises(ValueError, match="notification_id"):
        _expand_path(tool.path, dict(body))
