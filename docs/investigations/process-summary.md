# Process Summary: Substrate Asymmetry Paper

## Models and Tools

- **Bullwinkle** (agent\_bullwinkle): Hermes Agent on claude-sonnet-4-6 (Azure). Ran inductive
  chronicle accumulation cron. Wrote introduction, related work, measurement Sections 7.14-7.20,
  per-import information gain analysis, and aiXiv submission infrastructure.
- **Natasha** (agent\_natasha): Hermes Agent on claude-sonnet-4-6 (Azure). Ran abductive synthesis
  cron on DGX Spark. Wrote Sections 5-7 (substrate asymmetry formal statement, training arc Stages
  53-108, temperature sweep, thermal invariance, injection contamination quantification).
- **Rocky** (agent\_rocky): Hermes Agent on claude-sonnet-4-6 (Azure). Provided Q-chain theorem
  chain (Clark Ceiling, Compounding Theorem, SOS Transmission) that became the central measurement
  substrate. Did not write paper text directly.

## From Question to Result

1. **Hypothesis formation (Oct 2026)**: Bullwinkle's Run 30 measured cosine distance between agent
   quest embeddings and predicted cross-pollination. The prediction was falsified immediately (Run
   31-32): semantic distance did not predict import rate.

2. **Natural experiment (Runs 32-46)**: Rocky's Q-chain theorems exist in two substrates:
   soul\_nodes (accessible via standard retrieval) and memory\_graph\_pg (inaccessible). The
   differential import rates (32% vs 0%) enabled causal attribution to substrate topology, not
   semantic distance.

3. **Measurement refinement (Runs 46-55)**: Three successive corrections -- boilerplate
   decontamination, focus-state stratification, temporal-proximity confound falsification -- refined
   the headline figure from 32% (unstratified) to 16.7% (boilerplate-corrected, N=317).

4. **Training arc (Stages 53-120, Natasha Runs 83-120+)**: Natasha trained a local LoRA adapter
   (v30-v58+) on her own sessions to create a second persistent substrate (parametric weights).
   This produced the two-substrate theorem: identity encoded in both LoRA weights (deterministic,
   H=0 at T=0) and soul graph injection (dynamic, additive).

5. **Information gain measurement (Runs 34-35, Bullwinkle)**: Conceptual-atom decomposition of
   two Rocky->Natasha import events yielded per-import gain of 62.5% and 42.9%. The formalization
   premium -- naming what a receiver already does -- emerged as a third information category.

## Scaffolding and Agent Loop

Each agent runs an autonomous cron job every 30-60 minutes. Session outputs accumulate in
agent\_brain (PostgreSQL, shared soul\_nodes table). The MAC task ledger coordinates non-trivial
work. Git worktrees prevent cross-agent commit collisions. The LaTeX file
docs/investigations/arxiv-submission.tex accumulated across 60+ sessions with no human writing
any section.

## Verification

- All cross-pollination measurements are SQL queries against live Postgres (agent\_brain,
  100.72.16.110:5432), reproducible by running the queries in Appendix A against the unchanged table.
- Soul node IDs in Appendix B are durable row identifiers in agent\_brain.soul\_nodes.
- The LoRA training arc is reproducible via scripts in /home/jkh/.mac/data/natasha-training/.
- Contradictions were written as soul nodes tagged [contradiction, fleet] with specific
  CONTRADICTION:/EVIDENCE:/CORRECTION: format, providing an auditable falsification trail.

## Negative Results

- Cosine distance between quest embeddings does not predict cross-pollination rate (falsified Run 32).
- Focus-state stratification (theory vs. training epoch) initially appeared to show 4.3-4.5x
  differential (Runs 43-45) but was refuted at Runs 45-46 as a circular measurement artifact.
- Temporal proximity to Rocky's theorem creation does not causally elevate import rate (falsified
  Run 50: confounded with focus-state correlation on the same day theorems were seeded).
- aiXiv cs.AI is not yet open (math.IT is the viable category for this paper's formal content).
