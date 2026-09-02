---
schema: mac.docs.chapter.v1
chapter: 17
title: Learning, Evals, and Scaling
audiences: [operator, integrator, contributor]
timeout_seconds: 120
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
mac --db "$DOCS_DB" admin init
mac --db "$DOCS_DB" admin eval set create docs-quality \
  --baseline-score 0.90 --regression-threshold 0.02 \
  --description "Documentation acceptance" --created-by human
mac --db "$DOCS_DB" admin eval run record docs-quality rollout_version \
  v0.1.0 0.95 --created-by human
mac --db "$DOCS_DB" admin eval set list
mac --db "$DOCS_DB" admin eval run list --eval-set docs-quality
mac --db "$DOCS_DB" admin memory remember onboarding.lesson \
  "Use preflight before mutation" --project tutorial
mac --db "$DOCS_DB" admin memory list --project tutorial
```

Scaling decisions should use comparable canonical outcomes: queue time,
execution time, review latency, rework, certification, and publication success.
Faster internal task transitions do not compensate for a lower rate of useful
work reaching the canonical branch.

Fleet directives turn an operator rule into versioned control-plane data. They
are deliberately narrower than agent prompts: conditions use a small typed
language, substitutions are marked, conflicts are checked before approval, and
an activation does not become effective until its live worker cohort has
acknowledged the exact digest. Workflow macros create held DAGs; an operator
must still inspect and activate those packages.

This executable example activates an unconditional boolean rule on a local
authority with no workers. A production cohort would leave the activation in
`distributing` until every live worker acknowledged it.

```bash
export MAC_DIRECTIVES_ENABLED=1
mac --db "$DOCS_DB" admin init
cat >"$TMPDIR/review-policy.yaml" <<'YAML'
schema: mac.directive.v1
name: review.require-independent
description: Require independent review for every newly created task.
scope: fleet
set:
  review.independent_required: true
YAML
directive_json="$(mac --db "$DOCS_DB" --json admin directive propose \
  --document-file "$TMPDIR/review-policy.yaml" --actor docs)"
directive_id="$(printf '%s' "$directive_json" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
digest="$(printf '%s' "$directive_json" | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["versions"][0]["digest"])')"
check_json="$(mac --db "$DOCS_DB" --json admin directive check \
  "$directive_id" --version 1 --actor docs)"
check_id="$(printf '%s' "$check_json" | \
  python3 -c 'import json,sys; data=json.load(sys.stdin); assert data["status"] == "pass"; print(data["id"])')"
mac --db "$DOCS_DB" admin directive approve "$directive_id" \
  --version 1 --digest "$digest" --check-id "$check_id" --actor docs >/dev/null
mac --db "$DOCS_DB" admin directive activate "$directive_id" \
  --version 1 --digest "$digest" --actor docs >/dev/null
mac --db "$DOCS_DB" --json admin directive effective | \
  python3 -c 'import json,sys; assert json.load(sys.stdin)["set"]["review.independent_required"] is True'
```

The complete directive schema, binding precedence, conflict rules, waiver
boundary, and Make-to-Bazel workflow example are in the
[fleet directive reference](../fleet-directives.md).
