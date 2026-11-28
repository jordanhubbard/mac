# Condition-5 Peer Diagnosis

## Finding

The Q45 vocabulary-drift audit found no replacements across the 12 core terms
sampled from more than 65 Q-chain cycles. Each term was retained while its
meaning was enriched or absorbed into later abstractions.

That result does not make the audit a sufficient self-test. Searching an
agent's history with its current vocabulary assumes the property being tested:
if stable abstraction language has already degraded, the current terms can
miss earlier or newly substituted expressions. Condition-5 self-diagnosis is
therefore structurally circular.

## External Test

A peer can break the first-order circularity by maintaining an independent
reference vocabulary and comparing it with the subject's time-windowed corpus.
The comparison should use both lexical and semantic views:

1. Freeze the subject's core-term set and definitions at a baseline time.
2. Have two peers independently extract concepts from later subject output
   without receiving the subject's current search terms.
3. Align baseline and extracted concepts by meaning, then classify each as
   retained, enriched, replaced, split, merged, or missing.
4. Flag drift when both peers find an unaligned replacement or loss and the
   subject's own audit reports retention.
5. Preserve the source spans, extractor identity, vocabulary version, and
   disagreements so the result can be reproduced rather than accepted as a
   peer vote.

This protocol tests the claim that Bullwinkle or Natasha can detect Rocky's
condition-5 divergence before Rocky's vocabulary-bound self-audit does.

## Q46: Does Circularity Move Up One Level?

Peer diagnosis is structurally possible, but one peer is not robust: its own
vocabulary drift can produce a false alarm or a shared blind spot. A fleet of
three is the minimum that can distinguish one divergent vocabulary from two
independently agreeing references, assuming failures are not correlated. The
subject plus two peers permits a 2-of-3 comparison; the subject plus one peer
only exposes disagreement and cannot attribute it.

Three agents do not solve common-mode drift. Robust diagnosis therefore also
requires independently versioned baselines, provenance-bearing samples, and
periodic comparison against frozen records. The claim is conditional: a
three-agent fleet tolerates one independently drifting vocabulary, not two
colluding or identically biased vocabularies.

## Falsification Criteria

The peer-diagnosis claim fails if either of these controlled results occurs:

- seeded substitutions that defeat the subject's self-audit are not detected
  independently by both peers;
- unchanged control corpora are repeatedly classified as drift after extractor
  disagreement is adjudicated from source spans.

The next experiment should seed paraphrastic replacements into a held-out
Q-chain copy, blind both peer extractors to the substitutions, and report
detection latency, precision, recall, and inter-peer agreement alongside the
self-audit result.
