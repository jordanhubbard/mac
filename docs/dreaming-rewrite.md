# Dreaming, rewritten

`mac.dreaming` replaces `dream_scanner`, `dream_cycle_classifier` and
`dream_repair_tasks`. This note records why, so the next person does not
rebuild the thing that was removed.

## What the old cycle did, measured

Live hub ledger, 2026-07-28, after 40 days of production:

| metric | value |
|---|---|
| `dream:failure_pattern` rows | 154,273 |
| distinct summaries among them | 4,414 (a 35:1 duplication ratio) |
| `nap_summary` rows | 154,324 (4,540 distinct bodies) |
| share of the entire memory store | 97.6% |
| artifacts recording something that **worked** | 0 |
| raw evidence that was a success | 1,443 of ~7,020 (21%) |
| findings at `high` confidence | 111,031 — none of which triggered any action |
| repair tasks filed | 1,259 |
| repair tasks completed | 4 |
| dream artifacts actually embedded (i.e. retrievable) | 253 |

Five investigations in this directory triaged individual findings. All five
returned NO LIVE DEFECT or NOT ACTIONABLE; two failed specifically because the
sole evidence was a *success* record that the pipeline had labelled a failure.

## The root cause

The upstream feature this was modelled on is memory *curation*: read a memory
store plus past transcripts, emit a **new, smaller** store with duplicates
merged and stale entries replaced, leave the input untouched, and attach the
output to future sessions.

What got built was a defect scanner: regex over rows, append findings to the
live store, file bug tickets. The word "failure" does not appear anywhere in
the upstream design. That single substitution explains every symptom — wins are
invisible to a defect scanner, defect signatures are repo-specific so nothing
generalised beyond `mac`, and a scanner has no reason to ever remove a row.

The deepest inversion: dreams exist to fix a store that accumulates duplicates
and stale entries. The old cycle became, by a wide margin, the largest producer
of exactly that.

## What the rewrite does

Pipeline (`mac.dreaming.engine.dream`):

```
freeze → extract → resolve → compress → gate → ready_for_review
                                             ↘ quarantined
```

Five things are different in kind, not degree:

1. **Copy-on-write.** `save_run` only ever writes `dream_runs` /
   `dream_candidate_entries`. `memory_records` is touched by exactly one
   function, `promote_run`, and only after gates pass and someone asks.
   A bad dream is discarded, not cleaned out of live memory afterwards.

2. **Shrinking is a hard gate.** `compression_gate` quarantines any run whose
   output exceeds `max_output_ratio` (default 0.75) of its input. A dream that
   grows the store cannot be promoted. Promotion also retires the rows each
   candidate supersedes, so adoption is net-negative on row count.

3. **Wins are first-class.** `MemoryKind.PRACTICE` is a peer of `PITFALL`.
   `balance_gate` reports the mix on every run so a regression to
   failure-only output is visible immediately rather than after 40 days.

4. **Confidence means corroboration.** `MemoryCandidate.source_count` counts
   *distinct origins*, so three references to one session is one source. The
   old scorer counted rows, and the consolidator wrote a fresh row per pass,
   so re-reading one observation three times scored `high`.

5. **Conversations are judged as wholes.** `SessionReflection` records the
   inferred objective and whether it was met, abandoned, derailed or left
   unresolved — the question the old cycle had no representation for.

Plus: extraction is a model call through the existing router seam
(`resolve_model_caller`), the heuristic fallback reads only *structured*
outcome fields and never keyword-scans prose, project comes from the data
rather than a hardcoded `mac.*` table, the pipeline's own output is excluded
from its input so it cannot feed on itself, and promoted memories are rendered
into the executor prompt by `recall_deployment_lessons` — the loop the old
artifacts never closed.

## Gates

`provenance_coverage`, `contradiction_reduction`, `privacy`,
`retrieval_quality`, `compression`, `win_balance` (advisory).

The old implementation built exactly one of these — the privacy filter — and
built it well. Its regexes are carried over near-verbatim in
`mac.dreaming.redact`. The three quality gates it skipped are the three that
would have caught the 154,273 duplicates.

## Bounded history

The nap cycle runs a dream per agent per nap, so the run tables would grow
without limit — the same failure this rewrite exists to fix, one level up.
`prune_runs` keeps the most recent N per state (200 promoted, 50
ready-for-review, 50 quarantined, 20 discarded) and deletes their candidate
entries with them, and `run_dream_cycle` calls it every pass. Promoted runs are
kept longest because they are the provenance for memories that are live.

## Verified against real data

1,200 live `deployment_learning` rows through the heuristic extractor:

```
1200 in -> 171 out (14%)   all six gates pass
kinds: 5 practice / 166 pitfall
supersessions queued: 326  (promotion would be net -155 rows)
```

The heuristic path produces thin practice statements because it can only read
structured fields; substantive wins need the model path. That is a known limit
of the fallback, not of the design.

## Promotion

`run_dream_cycle` auto-promotes by default: a run that cleared **every** gate is
adopted immediately, writing `dream_memory:*` records and retiring the rows they
supersede. A quarantined run is never promoted — it stays readable for
inspection. Set `MAC_DREAM_AUTO_PROMOTE=0` and restart to turn this off on a
live fleet without a redeploy.

Retirement is the only irreversible step in the pipeline, and under
auto-promotion it runs unattended, so `promote_run` caps deletions at
`MAX_RETIRE_PER_RUN` (500). Hitting the cap halts retirement, reports
`retire_capped: true`, and still completes the promotion — one malformed run
cannot quietly empty the store.

Auto-promotion is what closes the loop: promoted memories are rendered into the
executor prompt by `recall_deployment_lessons`. Without it the pipeline produces
candidates that nothing ever reads — which is where it sat for its first 50
production runs.

## Operating it

```
mac dream run --project mac --limit 2000     # curate; writes a candidate store
mac dream show <run_id>                      # gates, stats, candidates
mac dream promote <run_id>                   # adopt; retires superseded rows
mac dream discard <run_id> --reason "..."    # throw it away
```

The nap cycle calls `run_dream_cycle` automatically and never auto-promotes.
