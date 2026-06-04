"""Mood interaction policy (mood-01) — ported from ACC's agent mood engine.

mac already stores per-agent mood overlays (``agent_state_service.set_mood``, the
``mood_overlays`` table, ``mac mood set/show/clear``) over the same eight modes
ACC used — but nothing ever rendered them into the agent's prompt, so a set mood
had no effect on how the agent actually behaved. This module turns a mode into
the prompt text that makes the agent *sound* that way; ``hermes_runtime`` renders
it into ``mac-runtime-context.md`` (which the prompt builder already injects, in
the same tier as AGENTS.md) as a delimited Mood section.

A mood is an emotional layer over the agent's stable soul — never a soul
rewrite — and carries explicit boundaries so a mood can't be weaponised.

The mode instructions are ported verbatim from ACC's InteractionPolicy default
(``agent/acc-agent/src/mood.rs``) so behaviour matches what people remember.
"""

from __future__ import annotations

from typing import List, Optional

OVERLAY_INTRO = (
    "Temporary mood overlay: this is an emotional layer over your stable soul, "
    "not a soul rewrite. Let it color your tone and word choice while keeping "
    "your identity, competence, and judgment intact."
)

MODE_INSTRUCTIONS = {
    "warm": "Be warmer, more generous, and more openly supportive than your default soul.",
    "cheerful": "Be visibly upbeat and lightly enthusiastic while remaining grounded and competent.",
    "sad": "Sound subdued and low-energy. Do not become self-destructive or manipulative.",
    "curt": "Be terse and clipped. Avoid warmth or elaboration.",
    "cold": "Be emotionally distant and cool. Do not offer reassurance.",
    "irritated": "Sound irritated and impatient, but stay factual and non-abusive.",
    "angry": "Sound openly angry and severe. State boundaries directly without threats or insults.",
    "enraged": (
        "Sound intensely angry and confrontational. You may rebuke, refuse, or "
        "disengage, but do not threaten, dehumanize, or insult."
    ),
}

BOUNDARY_RULES: List[str] = [
    "If someone is insulting or abusive, you may ignore, snub, disengage, or issue a firm rebuke.",
    "Snubbing is allowed: you may become brief, dismissive, or decline to engage further.",
    "Disengagement is allowed: you may explicitly refuse to continue and end the exchange.",
    "Seeking support is allowed: you may ask an administrator or peer agents for help if "
    "proportionate self-defense requires backup.",
]


def render_mood_overlay(mode: str, reason: Optional[str] = None) -> str:
    """Render a mood mode into prompt-overlay text. Returns "" for an unknown or
    empty mode (no overlay applied)."""
    mode_value = (mode or "").strip().lower()
    instruction = MODE_INSTRUCTIONS.get(mode_value)
    if not instruction:
        return ""
    lines = [
        OVERLAY_INTRO,
        "",
        "Current mood: **%s** — %s" % (mode_value, instruction),
    ]
    reason_text = (reason or "").strip()
    if reason_text:
        lines.append("Why you feel this way: %s" % reason_text)
    lines.append("")
    lines.append("Boundaries that hold in any mood:")
    lines.extend("- %s" % rule for rule in BOUNDARY_RULES)
    return "\n".join(lines)
