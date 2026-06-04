"""Mood engine: a stored mood overlay (mac mood set) must render into prompt
text and splice into the agent's runtime context so it actually changes how the
agent behaves — the piece that was missing (moods were stored but inert)."""
from __future__ import annotations

from mac import mood_policy
from mac.hermes_runtime import (
    MOOD_SECTION_BEGIN,
    MOOD_SECTION_END,
    refresh_mood_section,
    render_mood_section,
)


def test_policy_covers_exactly_the_model_mood_modes():
    # the rendered instructions must stay aligned with the MoodMode enum the
    # store validates against, or a settable mood would have no overlay text.
    from mac.models import MOOD_MODES

    assert set(mood_policy.MODE_INSTRUCTIONS) == set(MOOD_MODES)


def test_render_overlay_for_each_mode():
    for mode in mood_policy.MODE_INSTRUCTIONS:
        out = mood_policy.render_mood_overlay(mode)
        assert mode in out
        assert "emotional layer over your stable soul" in out  # not a soul rewrite
        assert "Boundaries that hold in any mood" in out        # can't be weaponised


def test_unknown_or_empty_mode_renders_nothing():
    assert mood_policy.render_mood_overlay("ecstatic") == ""
    assert mood_policy.render_mood_overlay("") == ""
    assert mood_policy.render_mood_overlay(None) == ""


def test_reason_is_included():
    out = mood_policy.render_mood_overlay("irritated", reason="the deploy broke twice")
    assert "irritated" in out and "the deploy broke twice" in out


def test_render_section_with_and_without_overlay():
    sec = render_mood_section({"mode": "warm", "reason": "good chat"})
    assert MOOD_SECTION_BEGIN in sec and MOOD_SECTION_END in sec
    assert "## Mood" in sec and "warm" in sec and "good chat" in sec
    # no active mood -> empty delimiters (so a refresh clears any stale block)
    assert render_mood_section(None) == "%s\n%s" % (MOOD_SECTION_BEGIN, MOOD_SECTION_END)
    # unknown mode -> also empty
    assert render_mood_section({"mode": "ecstatic"}) == "%s\n%s" % (MOOD_SECTION_BEGIN, MOOD_SECTION_END)


def test_refresh_is_idempotent_replaces_and_clears(tmp_path):
    md = tmp_path / "mac-runtime-context.md"
    md.write_text("# Runtime\n\nsome operational context\n", encoding="utf-8")

    refresh_mood_section(md, render_mood_section({"mode": "curt"}))
    t1 = md.read_text()
    assert "curt" in t1 and t1.count(MOOD_SECTION_BEGIN) == 1

    # switching moods replaces the block in place — never duplicates
    refresh_mood_section(md, render_mood_section({"mode": "warm"}))
    t2 = md.read_text()
    assert t2.count(MOOD_SECTION_BEGIN) == 1 and "warm" in t2 and "curt" not in t2
    assert "some operational context" in t2  # operator context preserved

    # clearing the mood leaves an empty block, no lingering mode text
    refresh_mood_section(md, render_mood_section(None))
    t3 = md.read_text()
    assert t3.count(MOOD_SECTION_BEGIN) == 1 and "Current mood" not in t3
