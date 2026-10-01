---
schema: mac.docs.chapter.v1
chapter: 14
title: Qualified Images and Synchronized Cutover
audiences: [operator, contributor]
timeout_seconds: 60
---

# Qualified Images and Synchronized Cutover

Controller source identity and runtime-image identity are related but different.
A deterministic runtime input digest identifies the reviewed build recipe.
Publication binds it to one immutable multi-architecture OCI digest, verifies
anonymous pull and platform manifests, and records GitHub provenance. CI tags
the digest `tested-<inputs-sha>` once every code gate passes; pin a host's
runtime image only to a digest that carries that tag.

```bash
python3 "$DOCS_ROOT/scripts/image-publication-identity.py" --help >/dev/null
python3 "$DOCS_ROOT/scripts/verify-runtime-publication.py" --help >/dev/null
test -x "$DOCS_ROOT/scripts/fleet-update"
```

Moving the fleet to a new commit is a separate, human-run step:
`scripts/fleet-update` updates the hub, then one worker at a time, and stops at
the first host that does not come back healthy on the new commit (see
[Updating the fleet with fleet-update](../operations/fleet-update.md)). The
synchronized release epochs and the deploy script that used to do this are
deleted.
