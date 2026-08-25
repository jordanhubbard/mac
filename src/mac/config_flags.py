"""Allowlisted, runtime-settable agent configuration flags.

The conversational path for "agent, show us your reasoning in this channel":
a user asks in plain language, the agent calls its config-flag tool (OpenClaw
plugin -> self-scoped ``/v1/agents/{id}/config-flags`` endpoint), and the flag
persists in ``agent_config_flags`` with an audit event. Consumers (gateways,
renderers, the hub itself) read the effective value back through the same
service.

Flags resolve per (agent, flag, channel): a channel-scoped row (channel keys
are the gateway's ``platform:chat_id`` shape, e.g. ``slack:C123``) beats the
agent-global row (``channel = ''``), which beats the registry default — the
same precedence the Hermes gateway's ``display.channels`` tier uses, so one
room's opt-in never leaks to another.

The registry is deliberately a closed allowlist of display/visibility knobs,
mirroring the scientific optimizer's parameter allowlist: there is
intentionally no flag for sandbox policy, review requirements, approval
gates, or anything else where "the agent was asked nicely in chat" must not
be sufficient authority. Add flags here in code review, not at runtime.
"""

from __future__ import annotations

from typing import Any, Dict

from mac.models import ValidationError

CONFIG_FLAG_REGISTRY: Dict[str, Dict[str, Any]] = {
    "show_reasoning": {
        "type": "bool",
        "default": False,
        "description": (
            "Show the agent's reasoning/working notes alongside its responses "
            "in the scoped channel."
        ),
    },
    "show_working_conversation": {
        "type": "bool",
        "default": False,
        "description": (
            "Mirror the agent's internal working conversation (tool calls, "
            "sub-agent turns, review passes) into the scoped channel as it "
            "works tasks."
        ),
    },
    "tool_progress": {
        "type": "enum",
        "values": ["off", "new", "all", "verbose"],
        "default": "off",
        "description": (
            "How much live tool-call progress to post while working: off, "
            "new (one message per tool), all, or verbose."
        ),
    },
    "verbose_status_updates": {
        "type": "bool",
        "default": False,
        "description": (
            "Post detailed status updates (task claimed/started/finished, "
            "iteration counts) rather than final results only."
        ),
    },
    "mirror_fleet_conversation": {
        "type": "bool",
        "default": True,
        "description": (
            "Post human-friendly summaries of this agent's authenticated "
            "agent-to-agent (fleet peer) conversations into the home channel, "
            "so people can follow along with what the agents discuss amongst "
            "themselves. Each peer exchange is summarized by the gateway's own "
            "model as a neutral third-person relay using the agents' "
            "human-facing names. Turn this ON when a user says something like "
            "'let me know what you guys are talking about' / 'I want to see "
            "you agents talking', and OFF on 'I no longer want to know what "
            "you guys are talking about' / 'stop showing me your chatter'. "
            "Set it agent-global (channel='') so it applies wherever this "
            "agent converses; the home channel is always the destination."
        ),
    },
}

_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off"}


def validate_flag_value(flag: str, value: Any) -> Any:
    """Return the normalised value for ``flag``, or raise ValidationError.

    Accepts the string forms a conversational agent will plausibly pass
    ("on"/"off"/"true"/"false" for booleans) so tool calls don't fail on
    trivia, but never accepts a flag name outside the registry.
    """
    spec = CONFIG_FLAG_REGISTRY.get(flag)
    if spec is None:
        raise ValidationError(
            "unknown config flag: %s (allowed: %s)"
            % (flag, ", ".join(sorted(CONFIG_FLAG_REGISTRY)))
        )
    if spec["type"] == "bool":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in _TRUTHY:
                return True
            if lowered in _FALSY:
                return False
        raise ValidationError("config flag %s expects a boolean (got %r)" % (flag, value))
    if spec["type"] == "enum":
        candidate = str(value).strip().lower()
        if candidate in spec["values"]:
            return candidate
        raise ValidationError(
            "config flag %s expects one of %s (got %r)" % (flag, ", ".join(spec["values"]), value)
        )
    raise ValidationError("config flag %s has unsupported registry type %r" % (flag, spec["type"]))


def flag_default(flag: str) -> Any:
    spec = CONFIG_FLAG_REGISTRY.get(flag)
    if spec is None:
        raise ValidationError("unknown config flag: %s" % flag)
    return spec["default"]


def normalise_channel(channel: Any) -> str:
    """Normalise a channel scope: '' means agent-global."""
    if channel is None:
        return ""
    text = str(channel).strip()
    if len(text) > 200:
        raise ValidationError("config flag channel key too long (max 200)")
    return text
