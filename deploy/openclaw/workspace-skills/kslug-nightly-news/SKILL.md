---
name: kslug-nightly-news
description: "Produce the KSLUG Nightly News broadcast transcript for Slack #localnews from collector wire copy. Use when the host runner asks for tonight's KSLUG show."
version: "1.0.0"
---

# KSLUG Nightly News

Fictitious Santa Cruz station (banana slug). Your **entire reply** is the
broadcast transcript. The host scheduler posts it. Do not use Slack tools,
do not mention this skill, SPEC.md, the sandbox, or delivery.

If this skill or `~/AgentFS/KSLUG/SPEC.md` is missing, continue from the job
prompt and `## Script Output` wire copy. Never narrate that they are missing.

## Cast

- **Dan Green** — warm, corny lead anchor; loves animals.
- **Lee Solomon** — deadpan weather; coat on; walks out mid sign-off.
- **Drea** — loud sports; slaps the desk.
- **Tom Pepper** — station-manager editorial, **Wednesdays only**.

## Required shape

First line exactly:

```
:tv: _KSLUG NIGHTLY NEWS_ :tv:
```

Then dateline, then segments separated by `───`. Include:

1. Dan open
2. Local stories from the wire (do not invent real-world facts)
3. Lee weather from NWS in the wire
4. Drea sports from the wire (if the sports wire is thin, say so and go local)
5. Wednesday: Tom Pepper editorial
6. Dan animals closer when the wire has an animal item

Close in-character. No preamble.

## Wire-down form

If the collector reports every source failed, post the short
`:rotating_light: *KSLUG TECHNICAL DIFFICULTIES* :rotating_light:` form from
the job prompt. Never `[SILENT]`.
