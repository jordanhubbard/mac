---
schema: mac.docs.chapter.v1
chapter: 8
title: Plans, DAGs, and the Fast Lane
audiences: [operator, integrator, contributor]
timeout_seconds: 90
---

# Plans, DAGs, and the Fast Lane

Independent task loops work for small isolated jobs, but parallel repository
changes need explicit coordination. A work package freezes a versioned DAG,
assigns non-overlapping nodes under WIP limits, assembles accepted outputs,
certifies the exact combination, and lands one canonical candidate.

Dependencies already provide the simplest DAG. Here the second task is held
until the first completes; `task ready` exposes only dispatchable work.

```bash
mac --db "$DOCS_DB" admin init
mac --db "$DOCS_DB" project create dag-demo --active
first="$(mac --db "$DOCS_DB" --json task create \
  "Prepare the interface" --project dag-demo --kind report | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
mac --db "$DOCS_DB" task create "Use the interface" \
  --project dag-demo --kind report --dependencies "$first" >/dev/null
mac --db "$DOCS_DB" task ready --project dag-demo --limit 10
mac --db "$DOCS_DB" work-package list --project dag-demo
mac work-package admit --help >/dev/null
```

The managed fast lane uses the same guarantees for an atomic single-task
package. It is faster because planning and assembly are smaller, not because
review, certification, or canonical publication are weakened.

Package activation is fail-closed: incompatible credentials, unavailable
capabilities, stale epochs, or an unqualified certifier leave the graph held.
