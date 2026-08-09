# Notifier Configuration Guide

This guide explains how to configure the MAC fleet's notification delivery
system, set up platform bindings for Slack, Telegram, and Discord webhooks,
define event-to-platform routing rules, customise message templates, and
diagnose delivery failures.

---

## Overview

`NotifierService` is the delivery bridge between MAC's internal task-progress
events (`operator_notifications` rows) and the human-facing channels your team
monitors. Every time a task completes, fails, or advances to a review state the
hub writes a notification record. The notifier service drains those records and
routes each one to one or more Hermes agents that then forward the message to
the correct Slack channel, Telegram chat, or other platform.

Key concepts:

| Term | Meaning |
|---|---|
| Notifier channel | A named routing rule: which event types go to which agent/binding |
| Platform binding | A link between a Hermes instance and an external chat identity (Slack channel, Telegram chat, etc.) |
| Hermes instance | A running Hermes process identified by its instance ID |
| Agent | A MAC worker agent; its `hermes_instance_id` links it to a Hermes instance |

Delivery is **pull-based**: a background tick calls `deliver_pending()` on a
schedule, claims notifications atomically, and marks them `delivered` or
`failed`. Stale claims (worker crashed mid-delivery) are automatically
reclaimed after a configurable timeout (default 600 s).

---

## Supported Channel Types

| Type | Delivery mechanism |
|---|---|
| `hermes` | Routed through a Hermes agent's `send_message` tool |
| `slack` | Routed through a Hermes agent that has a Slack platform binding |
| `telegram` | Routed through a Hermes agent that has a Telegram platform binding |

---

## 1. Slack Setup

### 1.1 Create a Slack App

1. Go to <https://api.slack.com/apps> and click **Create New App**.
2. Choose **From scratch**, give the app a name, and select your workspace.
3. Under **OAuth & Permissions** add the `chat:write` and (optionally)
   `chat:write.public` bot token scopes.
4. Install the app to your workspace and copy the **Bot User OAuth Token**
   (`xoxb-...`).
5. Invite the bot to the target channel: `/invite @<bot-name>`.

### 1.2 Register the Platform Binding

```console
mac platform-binding create \
  --hermes-instance-id <instance-id> \
  --platform slack \
  --external-id C0123456789 \    # Slack channel ID
  --display-name "#ops-alerts"
```

The `external_id` is the Slack **channel ID** (not the name). Find it by right-
clicking the channel in Slack and choosing "Copy link" — the last path segment
is the channel ID.

### 1.3 Configure the Notifier Channel

```console
mac admin notifier configure \
  --name ops-slack \
  --channel-type slack \
  --event-types "task.completed,task.failed" \
  --target '{"platform_binding_id": "<binding-id>"}'
```

Or via the Python API:

```python
cp.notifiers.configure_channel(
    "ops-slack",
    "slack",
    event_types=["task.completed", "task.failed"],
    target={"platform_binding_id": "<binding-id>"},
)
```

---

## 2. Telegram Setup

### 2.1 Create a Bot via BotFather

1. Open Telegram and start a chat with `@BotFather`.
2. Send `/newbot`, choose a display name and a `@username`.
3. Copy the **API token** (`123456:ABC-DEF...`).

### 2.2 Obtain the Chat ID

Forward any message from the target group/channel to `@userinfobot` to get the
numeric chat ID. For private channels, use the Telegram Bot API:

```
https://api.telegram.org/bot<TOKEN>/getUpdates
```

Send a message to the group first, then check the JSON response for
`message.chat.id`.

### 2.3 Register the Platform Binding

```console
mac platform-binding create \
  --hermes-instance-id <instance-id> \
  --platform telegram \
  --external-id "-1001234567890" \   # numeric chat ID (negative for groups/channels)
  --display-name "ops-channel"
```

### 2.4 Configure the Notifier Channel

```python
cp.notifiers.configure_channel(
    "ops-telegram",
    "telegram",
    event_types=["task.*"],      # wildcard: all task events
    target={"platform_binding_id": "<binding-id>"},
)
```

---

## 3. Discord Webhook Setup

Discord does not have a native `hermes` platform integration; use an incoming
webhook URL delivered through a `hermes` channel type.

1. In Discord, go to **Server Settings > Integrations > Webhooks** and create a
   new webhook for the target channel. Copy the webhook URL.
2. Store the URL as a MAC secret:
   ```console
   mac secrets set discord-webhook-url "<url>" \
     --scopes '{"agents": ["<hub-agent-id>"]}'
   ```
3. Configure a `hermes` notifier channel that targets the hub agent; the hub
   agent's system prompt or a cron skill is responsible for forwarding
   `status_update` messages to the Discord webhook.

---

## 4. Event-to-Platform Routing Rules

### 4.1 Event Pattern Syntax

The `event_types` field on a notifier channel accepts:

| Pattern | Matches |
|---|---|
| `task.completed` | Exactly the `task.completed` event |
| `task.*` | Any event whose type starts with `task.` |
| _(empty list)_ | Defaults to all events starting with `task.` |

Multiple patterns can be combined. A notification matches the channel if **any**
pattern matches its event type.

### 4.2 Routing by Channel Type

When a notification carries `channels: ["slack"]`, only notifier channels with
`channel_type = "slack"` (and optionally `"hermes"`) are considered. When
`channels: ["hermes"]` the service considers all enabled channels and also falls
back to auto-discovery (see Section 5).

### 4.3 Example: Separate Alert Tiers

```python
# Critical failures -> ops-team Telegram immediately
cp.notifiers.configure_channel(
    "critical-telegram",
    "telegram",
    event_types=["task.failed", "task.error"],
    target={"platform_binding_id": "<telegram-binding>"},
)

# All completions -> low-priority Slack digest channel
cp.notifiers.configure_channel(
    "completions-slack",
    "slack",
    event_types=["task.completed"],
    target={"platform_binding_id": "<slack-binding>"},
)

# Everything -> hub agent for structured logging
cp.notifiers.configure_channel(
    "all-hermes",
    "hermes",
    # event_types omitted -> defaults to task.*
    target={"agent_id": "<hub-agent-id>"},
)
```

---

## 5. Auto-Hermes Fallback

When a notification carries `channels: ["hermes"]` and **no** configured
notifier channel matches it, the service falls back to auto-discovery:

1. If the notification's `metadata.actor` resolves to a known agent with a
   `hermes_instance_id`, the service targets all platform bindings on that
   Hermes instance (Slack + Telegram only).
2. Otherwise, it fans out to **all** Slack and Telegram platform bindings
   registered on the fleet.

The fallback is intentionally broad so no notification is silently dropped when
channels have not been configured yet. Suppress it by disabling the `hermes`
channel in the notification or by registering an explicit notifier channel.

---

## 6. Template Customisation

Message payloads use the schema `mac.notifier.task_progress.v1`:

```json
{
  "schema": "mac.notifier.task_progress.v1",
  "status": "<event_type>",
  "notification": {
    "id": "...",
    "event_type": "task.completed",
    "subject_type": "task",
    "subject_id": "task_...",
    "title": "Task completed: ...",
    "body": "...",
    "channels": ["hermes"],
    "metadata": {},
    "status": "delivered",
    "created_at": "...",
    "delivered_at": "..."
  },
  "channel_type": "hermes",
  "target": {}
}
```

The Hermes agent that receives the `STATUS_UPDATE` message is responsible for
rendering it to the destination platform. Edit the agent's system prompt or a
cron skill to control formatting (Markdown, block-kit, etc.).

### Security: Secret Redaction

Before the payload leaves the service, `_message_safe_value` removes all keys
whose lowercase form appears in `FORBIDDEN_MESSAGE_KEYS` (`command`, `exec`,
`script`, `shell`, etc.) from every nested dict. The key is renamed to
`<key>_text` to preserve the value without the dangerous key name. This prevents
a misbehaving agent or malicious notification body from smuggling a job
specification through the message channel.

---

## 7. Debugging Delivery Failures

### 7.1 Inspect Notification Status

```console
mac notification list --status failed --limit 20
mac notification show <notification-id>
```

Status values:

| Status | Meaning |
|---|---|
| `pending` | Not yet attempted |
| `delivering` | Claimed by a worker; in progress |
| `delivered` | At least one message sent successfully |
| `skipped` | No matching targets (check channel config) |
| `failed` | Delivery raised an exception |

### 7.2 Check Observability Logs

Delivery failures emit a `notifier.delivery_failed` observability event:

```console
mac admin observability events --name notifier.delivery_failed --limit 10
```

Successful deliveries emit `notifier.delivered` with the message IDs.

### 7.3 Validate Webhook Credentials

**Slack**: Test with `curl`:
```console
curl -X POST https://slack.com/api/chat.postMessage \
  -H "Authorization: Bearer xoxb-..." \
  -H "Content-Type: application/json" \
  -d '{"channel": "C0123456789", "text": "test"}'
```
Check `"ok": true` in the response. Common errors:
- `not_in_channel` — invite the bot to the channel first.
- `token_revoked` — regenerate the token in the Slack app dashboard.
- `channel_not_found` — use the channel ID (starts with `C`), not the name.

**Telegram**: Test with:
```console
curl "https://api.telegram.org/bot<TOKEN>/sendMessage" \
  -d chat_id=<CHAT_ID> \
  -d text="test"
```
Common errors:
- `400 Bad Request: chat not found` — bot not a member of the group; add it.
- `403 Forbidden` — bot was kicked; re-add.

### 7.4 Rate Limits

**Slack**: Tier-3 endpoints allow ~50 requests/minute per workspace. If the
fleet fires many notifications in a burst, some deliveries will receive `429
Too Many Requests`. The notifier marks these `failed`; the stale-claim retry
(default 600 s) will re-attempt them automatically.

**Telegram**: Global limit of 30 messages/second; per-group limit of 20
messages/minute. For high-volume fleets consider routing to a single hub agent
that batches messages.

### 7.5 Stale "delivering" Records

If a worker crashes between claiming and delivering, the notification stays
`delivering` indefinitely. The service automatically reclaims records whose
`delivered_at` is older than `DELIVERY_CLAIM_TIMEOUT_SECONDS` (600 s by
default) on the next `deliver_pending()` call. No manual intervention is
required; however, if you see persistent `delivering` rows older than 10
minutes, check that the notifier background tick is running:

```console
mac service status notifier
```

### 7.6 Skipped Notifications

A notification is `skipped` when `deliver_pending()` finds no targets. This
means either:
- No notifier channel is configured that matches the event type — add one.
- The channel's target specifies an agent ID that has no `hermes_instance_id`.
- All matching channels are disabled (`enabled=False`).
- The platform binding's Hermes instance has no registered agents.

Enable verbose logging to trace target resolution:

```console
MAC_LOG_LEVEL=debug mac admin notifier deliver --once
```

---

## 8. Quick Reference

```python
from mac.services import ControlPlane

cp = ControlPlane.in_memory()   # or load from config in production

# Create a Slack notifier channel
ch = cp.notifiers.configure_channel(
    "ops-slack",
    "slack",
    event_types=["task.completed", "task.failed"],
    target={"platform_binding_id": "<binding-id>"},
    enabled=True,
)

# List all enabled channels
for c in cp.notifiers.list_channels(enabled=True):
    print(c.name, c.channel_type, c.event_types)

# Trigger delivery manually (normally runs on a schedule)
result = cp.notifiers.deliver_pending(limit=100)
print(result["delivered"], "delivered,", result["failed"], "failed")

# Disable a channel temporarily
cp.notifiers.configure_channel("ops-slack", "slack", enabled=False)

# Delete a channel
cp.notifiers.delete_channel("ops-slack")
```
