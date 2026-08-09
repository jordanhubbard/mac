---
schema: mac.docs.chapter.v1
chapter: 11
title: Review, Publication, and Ref Hygiene
audiences: [operator, contributor]
timeout_seconds: 90
---

# Review, Publication, and Ref Hygiene

Review answers whether one exact attempt is acceptable. Publication answers
whether that accepted attempt became canonical. Keeping those questions
separate prevents an approved branch from being mistaken for merged work after
parallel development moves `main`.

The repository-reference reconciler tracks managed task refs and retires them
only when lifecycle, ancestry, publication, grace, and open-review rules agree.
Status is hub-owned; local audit is diagnostic and never invents completion.

```bash
mac --db "$DOCS_DB" admin init
mac --db "$DOCS_DB" admin repo refs audit --repo "$DOCS_REPO" --grace-days 7 >/dev/null
mac admin repo refs status --help >/dev/null
mac admin review auto-land --help >/dev/null
mac admin publish --help >/dev/null
```

When reviewed work diverges from current `main`, the correct method is guarded
reconciliation: replay or merge the reviewed head onto the observed canonical
tip, rerun required tests and CodeGraph analysis, push with a compare-and-swap
guard, then record the remotely verified final SHA. Divergence is expected in a
parallel system; unverified publication is not.

Use `mac admin repo refs status` during session closeout. Manual pruning is appropriate
only for refs the same lifecycle policy marks eligible.
