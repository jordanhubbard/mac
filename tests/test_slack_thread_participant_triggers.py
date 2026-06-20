from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def _adapter(extra=None):
    hermes_path = Path(__file__).resolve().parents[1] / "src" / "mac" / "_hermes"
    if str(hermes_path) not in sys.path:
        sys.path.insert(0, str(hermes_path))

    from gateway.platforms.slack import SlackAdapter
    from gateway.config import PlatformConfig

    adapter = SlackAdapter(PlatformConfig(enabled=True, extra=dict(extra or {})))
    adapter._bot_user_id = "UBOT"
    adapter._team_bot_user_ids["T1"] = "UBOT"
    adapter._team_bot_user_ids["T2"] = "UBOT"
    adapter._channel_team["C1"] = "T1"
    adapter._channel_team["C2"] = "T2"
    return adapter


async def _install_test_seams(adapter):
    seen = []

    async def _capture(event):
        seen.append(event)

    async def _user_name(_user_id, chat_id=None):
        return "Slack User"

    async def _thread_context(**_kwargs):
        return ""

    async def _parent_text(**_kwargs):
        return "Parent message"

    adapter.handle_message = _capture
    adapter._resolve_user_name = _user_name
    adapter._fetch_thread_context = _thread_context
    adapter._fetch_thread_parent_text = _parent_text
    return seen


def test_slack_thread_reply_after_top_level_mention_triggers_without_mention():
    async def run():
        adapter = _adapter({"require_mention": True})
        seen = await _install_test_seams(adapter)

        await adapter._handle_slack_message(
            {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "team": "T1",
                "user": "U1",
                "ts": "1710000000.000100",
                "text": "<@UBOT> can you track this?",
            }
        )

        assert len(seen) == 1
        assert adapter._has_bot_thread_participation("T1", "1710000000.000100")
        assert adapter._has_mentioned_thread("T1", "1710000000.000100")

        await adapter._handle_slack_message(
            {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "team": "T1",
                "user": "U2",
                "ts": "1710000001.000200",
                "thread_ts": "1710000000.000100",
                "text": "additional context from another participant",
            }
        )

        assert len(seen) == 2
        assert seen[1].text == "additional context from another participant"
        assert seen[1].source.user_id == "U2"
        assert seen[1].source.thread_id == "1710000000.000100"
        assert seen[1].reply_to_message_id == "1710000000.000100"

    asyncio.run(run())


def test_slack_strict_mention_does_not_auto_trigger_thread_replies():
    async def run():
        adapter = _adapter({"require_mention": True, "strict_mention": True})
        seen = await _install_test_seams(adapter)

        await adapter._handle_slack_message(
            {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "team": "T1",
                "user": "U1",
                "ts": "1710000000.000100",
                "text": "<@UBOT> can you track this?",
            }
        )

        assert len(seen) == 1
        assert not adapter._has_bot_thread_participation("T1", "1710000000.000100")
        assert not adapter._has_mentioned_thread("T1", "1710000000.000100")

        await adapter._handle_slack_message(
            {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "team": "T1",
                "user": "U2",
                "ts": "1710000001.000200",
                "thread_ts": "1710000000.000100",
                "text": "this should not trigger without another mention",
            }
        )

        assert len(seen) == 1

    asyncio.run(run())


def test_slack_thread_trigger_memory_is_workspace_scoped():
    async def run():
        adapter = _adapter({"require_mention": True})
        seen = await _install_test_seams(adapter)

        await adapter._handle_slack_message(
            {
                "type": "message",
                "channel": "C1",
                "channel_type": "channel",
                "team": "T1",
                "user": "U1",
                "ts": "1710000000.000100",
                "text": "<@UBOT> can you track this?",
            }
        )

        assert len(seen) == 1
        assert adapter._has_mentioned_thread("T1", "1710000000.000100")
        assert not adapter._has_mentioned_thread("T2", "1710000000.000100")

        await adapter._handle_slack_message(
            {
                "type": "message",
                "channel": "C2",
                "channel_type": "channel",
                "team": "T2",
                "user": "U2",
                "ts": "1710000001.000200",
                "thread_ts": "1710000000.000100",
                "text": "same timestamp in a different workspace should not trigger",
            }
        )

        assert len(seen) == 1

    asyncio.run(run())
