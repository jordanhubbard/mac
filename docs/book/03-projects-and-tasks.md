---
schema: mac.docs.chapter.v1
chapter: 3
title: Projects and Tasks
audiences: [user, operator, integrator]
timeout_seconds: 60
---

# Projects and Tasks

A project is the scheduling and policy boundary around related work. New
projects are paused unless explicitly activated. Tasks can be staged separately
with `--no-dispatch`; the hold lives at `metadata.no_dispatch=true` and is
removed by `task release`.

This example creates an active project, stages one report task, proves that the
task exists, then releases it for dispatch.

```bash
mac --db "$DOCS_DB" init
mac --db "$DOCS_DB" project create tutorial --active
task_id="$(mac --db "$DOCS_DB" --json task create \
  "Summarize the tutorial" --project tutorial --kind report \
  --no-dispatch --no-decompose | \
  python3 -c 'import json,sys; print(json.load(sys.stdin)["id"])')"
mac --db "$DOCS_DB" task show "$task_id" >/dev/null
mac --db "$DOCS_DB" task release "$task_id"
mac --db "$DOCS_DB" task ready --project tutorial --limit 10
```

Repository-backed projects use `mac project onboard URL --project NAME` to
create contract-authoring work. Once `.mac/project.yaml` exists in a hub-visible
checkout, register that checkout with `mac bridge repository register`.

Creating a task expresses intent; it does not grant an agent permission to work.
The claim and lease in the next chapter provide that fence.
