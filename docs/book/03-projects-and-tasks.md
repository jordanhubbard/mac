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

Repository-backed projects use one branch-qualified registration string:
`GIT_URL#BRANCH`. The fragment defaults to `main`, so these are equivalent:

```console
mac project register git@github.com:org/widget.git
mac project register git@github.com:org/widget.git#main
```

`register` creates the project record and its contract-authoring task. A second
registration for another branch is an internal project fork:

```console
mac project register git@github.com:org/widget.git#release/next
# default project name: widget@release/next
```

The URL-and-branch pair is unique. The same URL may be registered again only
with a different branch. Once `.mac/project.yaml` exists in a hub-visible
checkout, register that checkout with `mac bridge repository register`.
The checkout attachment is an internal execution detail; the operator-facing
resource remains the project.

The CLI provides project CRUD:

```console
mac project create manual-project
mac project list
mac project show widget
mac project update widget --branch release/next
mac project update widget --description "Release maintenance"
mac project unregister widget --force
```

The HTTP API exposes the same lifecycle:

```text
POST   /projects
POST   /projects/register
GET    /projects
GET    /projects/{project}
PUT    /projects/{project}
DELETE /projects/{project}?force=true
POST   /projects/{project}/dispatch
```

`POST /projects/register` accepts the existing repository-onboarding body; put
the canonical string in `repository_url`. `PUT /projects/{project}` accepts
`repository_registration` or `default_branch` in addition to name,
description, metadata, and status.

Creating a task expresses intent; it does not grant an agent permission to work.
The claim and lease in the next chapter provide that fence.
