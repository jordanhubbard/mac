# G7/7-D: Unused Module Constants Audit

**Task:** task_7821ba66112a44f28b48b60633f01c19  
**Audited at:** main @ 3caea81 (current HEAD: 4a5f357)  
**Auditor:** agent_bullwinkle  
**Date:** 2026-06-20  

## Scope

Audit four modules for unused module-level constants and remove any with
zero external references (per the verification protocol:
`grep -rn 'CONSTANT_NAME' src/mac tests` — expect only the definition
line for a constant to qualify as unused).

Files audited:
- `src/mac/mood_policy.py`
- `src/mac/soul_snapshot.py`
- `src/mac/nap_consolidator.py`
- `src/mac/hermes_chat_config.py`

## Findings

All module-level constants in these files are actively referenced.
**No constants were removed.**

### mood_policy.py

| Constant | Definition | Non-definition references | Decision |
|---|---|---|---|
| `OVERLAY_INTRO` | line 22 | line 59 (used in `render_mood_overlay`) | KEEP |
| `MODE_INSTRUCTIONS` | line 28 | line 55 (internal), `tests/test_mood_policy.py:20,24` (external) | KEEP |
| `BOUNDARY_RULES` | line 42 | line 68 (used in `render_mood_overlay`) | KEEP |

### soul_snapshot.py

| Constant | Definition | Non-definition references | Decision |
|---|---|---|---|
| `SOUL_FILES` | line 32 | line 173 (default param), line 8, 177 (docstrings) | KEEP |
| `MEMORY_FILES` | line 37 | line 174 (default param), line 182 (docstring) | KEEP |
| `SNAPSHOT_SCHEMA` | line 39 | line 189 (used in manifest), `tests/test_soul_snapshot.py:77` (external) | KEEP |

### nap_consolidator.py

| Constant | Definition | Non-definition references | Decision |
|---|---|---|---|
| `DREAM_SCHEMA` | line 58 | line 575 (used in `_normalize_dream_candidate`) | KEEP |
| `DREAM_RECORD_PREFIX` | line 59 | lines 326, 413 (used in record_type construction) | KEEP |
| `DREAM_KINDS` | line 60 | line 537 (validation in `_normalize_dream_candidate`) | KEEP |
| `DREAM_SCOPES` | line 67 | line 541 (validation in `_normalize_dream_candidate`) | KEEP |
| `_CONFIDENCE_SCORES` | line 68 | lines 156, 158, 159, 544, 548 (used in scoring) | KEEP |

### hermes_chat_config.py

| Constant | Definition | Non-definition references | Decision |
|---|---|---|---|
| `CHAT_ENV_KEYS` | line 38 | lines 80, 88 (used in `sync_hermes_env`) | KEEP |
| `_DEPLOY_MANAGED_IMAGE_PROVIDERS` | line 226 | line 270 (used in `ensure_image_gen_provider`) | KEEP |

## Conclusion

The architectural evaluation that generated this task (2026-06-18, against
main @ 3caea81) named `OVERLAY_INTRO`, `SOUL_FILES`, `DREAM_SCHEMA`, and
`CHAT_ENV_KEYS` as "named examples" of potentially unused constants.
A full audit confirms all four are actively used, as are all other
module-level constants in the four files.

No deletions are appropriate. The codebase is clean with respect to
dead module-level constants in these modules.
