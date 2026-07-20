---
schema: mac.docs.chapter.v1
chapter: 17
title: Learning, Evals, and Scaling
audiences: [operator, integrator, contributor]
timeout_seconds: 60
---

# Learning, Evals, and Scaling

Fleet learning converts operational outcomes into routing inputs. A recent
successful repository access attempt can prefer one reviewer; a newer
authentication failure temporarily avoids repeating the same broken credential
pattern. Memories are secret-free, scoped, and replaceable by later evidence.

Evals measure whether a candidate is safe to promote. They are not task counts
or subjective impressions: an eval set defines scoring direction, baseline, and
regression threshold, while each run binds a score to a rollout, environment,
or build.

```bash
mac --db "$DOCS_DB" init
mac --db "$DOCS_DB" eval set create docs-quality \
  --baseline-score 0.90 --regression-threshold 0.02 \
  --description "Documentation acceptance" --created-by human
mac --db "$DOCS_DB" eval run record docs-quality rollout_version \
  v0.1.0 0.95 --created-by human
mac --db "$DOCS_DB" eval set list
mac --db "$DOCS_DB" eval run list --eval-set docs-quality
mac --db "$DOCS_DB" memory remember onboarding.lesson \
  "Use preflight before mutation" --project tutorial
mac --db "$DOCS_DB" memory list --project tutorial
```

Scaling decisions should use comparable canonical outcomes: queue time,
execution time, review latency, rework, certification, and publication success.
Faster internal task transitions do not compensate for a lower rate of useful
work reaching the canonical branch.
