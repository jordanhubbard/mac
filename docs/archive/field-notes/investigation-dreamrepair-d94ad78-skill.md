!!! warning "Historical field note"
    This record preserves prior investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Investigation: dream finding `dreamrepair:d94ad78027c32d4825923f0ba91e9497` (skill)

**Task**: Assemble the ground-truth evidence record for a low-confidence
dream-cycle `failure_pattern` finding so its actionability can be judged from
the attached evidence alone. Review-only — no skills, tools, or source were
changed.
**Finding**: kind `failure_pattern`, scope `project`, project `mac`,
fingerprint `dreamrepair:d94ad78027c32d4825923f0ba91e9497`, confidence `low`,
`evidence_count = 1`, affected label `skill` (generic placeholder — no concrete
skill/tool/provider/`repo_area` name).
**Classifier signal that fired**: `\bskill[s]?\b` → area type `skill`
(`src/mac/dream_cycle_classifier.py:103`).
**Sole supporting record**: `mem_e6aaf8020ea7449c848dda273ef8928f`.
**Investigated by**: fleet worker (evidence-review node; no production code,
test, skill, or deploy edits).

## Verdict: NOT ACTIONABLE — single self-referential record, generic token label, no defect

The finding rests on exactly one supporting record,
`mem_e6aaf8020ea7449c848dda273ef8928f`, whose summary **echoes a prior
NOT ACTIONABLE probe** of an equivalent low-confidence `skill` finding rather
than reporting any new failure. There is no failing test, stack trace, error
signature, reproduction, or named offending skill/tool/provider behind it. The
`skill` label is a bare word-boundary token match on `\bskill[s]?\b`
(`src/mac/dream_cycle_classifier.py:103`), not a concrete defect location, and
the `low` confidence is the classifier's deterministic single-record structural
floor (`("low", 0.35)` at `src/mac/dream_cycle_classifier.py:87`), not a
diagnosed fault. This independently re-verifies the same ground truth reached
for the near-duplicate `skill` findings already closed NOT ACTIONABLE in
`docs/archive/field-notes/investigation-dreamrepair-c8dd8037-skill.md`,
`docs/archive/field-notes/investigation-dreamrepair-5404b15-skill.md`, and
`docs/archive/field-notes/investigation-dreamrepair-ffbc63f8-skill.md`.

No files under `src/`, `tests/`, `skills/`, or `deploy/` were modified by this
investigation.

## Evidence map

The evidence flows in one direction, finding → single supporting record, with no
independent corroboration entering at any hop:

1. **Finding** `dreamrepair:d94ad78027c32d4825923f0ba91e9497` is the stable
   dedupe key `repair_fingerprint()` hashes from the candidate's
   `{kind, scope, project, signature, summary, affected}`
   (`src/mac/dream_repair_tasks.py:175`). It is a dedupe key for a dream
   candidate, not a pointer into any single skill module.
2. **Supporting record** `mem_e6aaf8020ea7449c848dda273ef8928f` is the single
   evidence item the dream classifier attaches to the finding
   (`evidence_count = 1`). Its summary restates a previous NOT ACTIONABLE
   disposition of an equivalent generic `skill` finding; it therefore carries a
   prior dismissal, not a fresh failure observation.
3. **Affected label** `skill` is a generic area bucket produced by a plain
   regex match, not a concrete skill. `affected.tools`, `affected.providers`,
   and `affected.repo_areas` are empty.

## Why the `skill` label is only a generic regex match

`skill` is assigned by the first `_SKILL_PATTERNS` entry, the word-boundary
pattern `\bskill[s]?\b` (`src/mac/dream_cycle_classifier.py:103`), matched
case-insensitively against the combined `summary + observations +
record_type_counts` text. Because the supporting record's summary mentions the
token "skill" while describing a prior skill-finding disposition, the pattern
fires on that word alone. The result is an area *bucket* named `skill`, not an
identified skill module, tool, provider, or repo area. No `_SKILL_PATTERNS`
entry more specific than the bare token matched, and no tool/provider/repo_area
signal fired, so every discriminating field stays a generic placeholder.

## Why the summary echoes a prior NOT ACTIONABLE probe

The single supporting record does **not** report a broken skill. Its summary
restates the closure of an earlier, equivalent low-confidence `skill` finding
(the same shape already dispositioned NOT ACTIONABLE in the sibling notes cited
above). Treating a "this was already judged not actionable" note as fresh
failure evidence is a feedback loop: the pattern re-derives itself from its own
prior dismissal and adds zero net-new signal about any real defect. This is the
same self-referential structure the current tree treats as info-only rather
than failure signal:

- The scanner emits `skill:<name>` name candidates as `tool_or_skill_name` at
  `severity="info"`, with excerpt `"skill referenced: <name>"`, for every skill
  name that merely appears in session text (`src/mac/dream_scanner.py:258`).
- `DREAM_INVENTORY_ONLY_KINDS = frozenset({"tool_or_skill_name"})` marks that
  kind as pure name inventory rather than a failure signal
  (`src/mac/dream_repair_tasks.py:26`).
- `_is_inventory_only_candidate()` skips such candidates before the
  `low + affected` gate precisely because they are `severity="info"` with
  nothing to act on (`src/mac/dream_repair_tasks.py:216`), and the scan report
  records them as `status="skipped"`, `reason="inventory_only_kind"`
  (`src/mac/dream_repair_tasks.py:110`).

## Classifier signals that fired

- **Area signal**: `\bskill[s]?\b` → area type `skill`
  (`src/mac/dream_cycle_classifier.py:103`, within `_SKILL_PATTERNS`). A plain
  word-boundary match on the token `skill`/`skills`. It is a text match, not a
  named skill.
- **Confidence**: deterministic `low` floor
  `CONFIDENCE_THRESHOLDS["low"] = ("low", 0.35)`
  (`src/mac/dream_cycle_classifier.py:87`), returned by `_confidence_for(...)`
  whenever `evidence_count < 2` and no second distinct signal type or record
  type is present (`src/mac/dream_cycle_classifier.py:258`). With exactly one
  evidence record the finding is necessarily `low`; `medium` needs two, `high`
  needs three.
- **Affected label**: `skill` is a generic area bucket, not a concrete skill.
  `affected.tools`, `affected.providers`, and `affected.repo_areas` are empty.

Together these explain the finding's kind, scope, confidence, and label entirely
from the consolidation heuristics acting on one self-referential record. None of
it signals that a skill module misbehaves.

## Corroboration assessment: single record only

The finding has **one** supporting record and no independent corroboration:

- **Single record** — `evidence_count = 1`. `medium`/`high` confidence is
  structurally unreachable without a second, independent record
  (`src/mac/dream_cycle_classifier.py:258`).
- **Self-referential** — the lone record's summary echoes a prior NOT ACTIONABLE
  probe of an equivalent finding, so it cannot attest to a new defect; it
  corroborates only the earlier dismissal it restates.
- **No reproduction** — no failing test, no reproducer, and no concrete defect
  location (the `skill` label is a placeholder, not a module/tool/provider).

## Determination

- Do **not** open a skill-behavior repair from this finding: it is not
  actionable as a skill-subsystem defect.
- Treat the finding as **low-confidence / unsubstantiated**; it can be aged out
  or superseded once no additional independent evidence accumulates.
- Reopen only if a *named* skill or tool acquires a reproducible failure
  signature (a real error/stack/failing test) with at least two independent,
  non-self-referential evidence records.

## Assumptions recorded

- The originating hub was not reachable from the investigation sandbox, so the
  referenced memory record `mem_e6aaf8020ea7449c848dda273ef8928f` was not
  live-queried. Its identifier, the generic `skill` label, the single-record
  count, and the self-referential prior-disposition summary are taken from the
  task specification and corroborated against the current source tree, which is
  the same method used by the sibling `skill` field notes cited above.
