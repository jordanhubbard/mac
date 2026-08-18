---
schema: mac.docs.chapter.v1
chapter: 8
title: Plans and Task DAGs
audiences: [operator, integrator, contributor]
timeout_seconds: 90
---

# Plans and Task DAGs

Independent task loops work for small isolated jobs, but parallel repository
changes need explicit coordination. Task dependencies are how that
coordination is expressed: they form a directed acyclic graph the ledger
enforces, so downstream work cannot start before the work it relies on has
completed.

Here the second task is held until the first completes; `task ready` exposes
only dispatchable work.

```bash
mac --db "$DOCS_DB" admin init
mac --db "$DOCS_DB" project create dag-demo --active
first="$(mac --db "$DOCS_DB" --json task create \
  "Prepare the interface" --project dag-demo --kind report | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
mac --db "$DOCS_DB" task create "Use the interface" \
  --project dag-demo --kind report --dependencies "$first" >/dev/null
mac --db "$DOCS_DB" task ready --project dag-demo --limit 10
```

Ordering a graph does not weaken any other guarantee. Every node in it still
carries the same evidence, review, certification, and canonical publication
requirements as a standalone task; the dependency edges only decide *when* a
task becomes dispatchable, never *whether* its completion has been proven.
