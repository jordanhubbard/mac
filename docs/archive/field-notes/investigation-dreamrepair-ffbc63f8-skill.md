!!! warning "Historical field note"
    This record preserves prior investigation evidence for a single dream
    finding. It is not a current operating contract; use the numbered book and
    current runbooks for instructions.

# Investigation: dream finding `dreamrepair:ffbc63f8695e9316b064bb1f6d3566cb` (skill)

**Task**: Assemble the complete evidence record for a low-confidence
dream-cycle `failure_pattern` finding so its actionability can be judged from
the attached evidence alone. Review-only — no skills, tools, or source were
changed.
**Finding**: kind `failure_pattern`, scope `project`, project `mac`,
fingerprint `dreamrepair:ffbc63f8695e9316b064bb1f6d3566cb`, confidence `low`
(`confidence_score = 0.35`), `evidence_count = 1`, affected label `skill`
(generic placeholder — no concrete skill/tool/provider/`repo_area` name).
**Classifier signal that fired**: `\bskill[s]?\b` → area type `skill`
(`src/mac/dream_cycle_classifier.py:103`).
**Sole supporting record**: `mem_88ebe4bba2924b97b1843560a58884ee`
(record_type `deployment_learning:mac`), from repair task
`task_repair_a2b91fa4f146c4d1f879485c`.
**Origin dream memory**: `mem_75cf619496f94677aa31b0ad45d3adb2`, emitted by
nap run `nap_15e3fb5ce5cf4ae0a88b464d22939995`.
**Investigated by**: fleet worker (investigation node; no production code,
test, skill, or deploy edits).

## Verdict: NOT ACTIONABLE — single-record, info-only inventory signal, not a defect

The finding rests on exactly one supporting record whose own claim is that
`skill:<name>` dream candidates are **info-only inventory records**
(`severity="info"`), not failure evidence. There is no failing test, stack
trace, error signature, reproduction, or named offending skill/tool/provider
behind it. The `0.35` score is the classifier's deterministic single-record
structural floor (an evidence-volume signal), and the `skill` label is a bare
word-boundary token match on `\bskill[s]?\b`, not a concrete defect location.
This adopts and independently re-verifies the same ground truth reached for the
near-duplicate `skill` findings already closed NOT ACTIONABLE in
`docs/archive/field-notes/investigation-dreamrepair-c8dd8037-skill.md` and
`docs/archive/field-notes/investigation-dreamrepair-5404b15-skill.md`.

No files under `src/`, `tests/`, `skills/`, or `deploy/` were modified by this
investigation.

## Provenance chain (as retrieved)

The evidence flows in one direction, origin → supporting record → finding, with
no independent corroboration entering at any hop:

1. **Origin dream memory** `mem_75cf619496f94677aa31b0ad45d3adb2` was emitted by
   nap run `nap_15e3fb5ce5cf4ae0a88b464d22939995`. It is the consolidation-time
   dream artifact that seeds the low-confidence `failure_pattern` candidate.
2. **Supporting record** `mem_88ebe4bba2924b97b1843560a58884ee`
   (`deployment_learning:mac`) was authored by repair task
   `task_repair_a2b91fa4f146c4d1f879485c`. It is the single evidence item the
   dream classifier attaches to the finding (`evidence_count = 1`,
   `record_type_counts: {deployment_learning:mac: 1}`).
3. **Finding** `dreamrepair:ffbc63f8695e9316b064bb1f6d3566cb` is the stable
   dedupe key `repair_fingerprint()` hashes from the candidate's
   `{kind, scope, project, signature, summary, affected}`
   (`src/mac/dream_repair_tasks.py:175`). It is a dedupe key for a dream
   candidate, not a pointer into any single skill module.

## What the sole supporting record actually claims

`mem_88ebe4bba2924b97b1843560a58884ee` does **not** report a broken skill. Its
substantive claim is that `skill:<name>` dream candidates are **pure name
inventory, not a failure signal** — they are emitted at `severity="info"` for
every skill name that merely appears in session text, and carry no failure
excerpt or root signal. That claim is exactly the behavior the current tree
encodes and can be corroborated read-only against source:

- The scanner emits `skill:<name>` as a `tool_or_skill_name` candidate at
  `severity="info"`, with excerpt `"skill referenced: <name>"`, for every skill
  name extracted from session text (`src/mac/dream_scanner.py:256`). The tool
  variants (`tool:<name>`) are emitted the same way
  (`src/mac/dream_scanner.py:239`, `src/mac/dream_scanner.py:247`).
- `DREAM_INVENTORY_ONLY_KINDS = frozenset({"tool_or_skill_name"})` marks this
  kind as "pure name inventory rather than a failure signal"
  (`src/mac/dream_repair_tasks.py:20`).
- `_is_inventory_only_candidate()` skips such candidates before the
  `low + affected` gate precisely because they are `severity="info"` with
  "nothing to act on" (`src/mac/dream_repair_tasks.py:216`), and the scan report
  records them as `status="skipped"`, `reason="inventory_only_kind"`
  (`src/mac/dream_repair_tasks.py:109`).

So the record's content is a statement about classifier/scanner *inventory
semantics*, not an observation of a skill malfunction. Treating it as fresh
failure evidence corroborates only itself.

## Classifier signals that fired

- **Area signal**: `\bskill[s]?\b` → area type `skill`
  (`src/mac/dream_cycle_classifier.py:103`, within `_SKILL_PATTERNS`). This is a
  plain word-boundary match on the token `skill`/`skills`, matched against
  `summary + observations + record_type_counts` keys. It is a text match, not a
  named skill.
- **Confidence**: `confidence_score = 0.35`. That is the deterministic `low`
  floor `CONFIDENCE_THRESHOLDS["low"] = ("low", 0.35)`
  (`src/mac/dream_cycle_classifier.py:87`), returned by `_confidence_for(...)`
  whenever `evidence_count < 2` and no second distinct signal type or record
  type is present (`src/mac/dream_cycle_classifier.py:233`). With exactly one
  evidence record the finding is necessarily `low`; `medium` needs two, `high`
  needs three.
- **Affected label**: `skill` is a generic area bucket, not a concrete skill.
  `affected.tools`, `affected.providers`, and `affected.repo_areas` are empty.

Together these explain the finding's kind, scope, confidence, and label
entirely from the consolidation heuristics acting on one info-only inventory
record. None of it signals that a skill module misbehaves.

## Corroboration assessment: single record only

The finding has **one** supporting record and no independent corroboration:

- **Single record** — `evidence_count = 1`;
  `record_type_counts: {deployment_learning:mac: 1}`. `medium`/`high` confidence
  is structurally unreachable without a second, independent record.
- **Self-referential / info-only** — the lone record's own claim is that these
  `skill:<name>` candidates are info-only inventory, so it cannot attest to a
  new defect; it corroborates only the inventory semantics it describes.
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

- The originating hub was not reachable from the investigation sandbox
  (`mac memory search` returned `HTTP 403 Forbidden`), so the three referenced
  memory records were not live-queried. Their identifiers, record types,
  provenance, and quoted claim are taken from the task specification and
  corroborated against the current source tree, which is the same method used
  by the sibling `skill` field notes cited above.
