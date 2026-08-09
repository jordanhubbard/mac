---
schema: mac.docs.chapter.v1
chapter: 6
title: Hermes and the Fleet IDE
audiences: [user, operator, integrator]
timeout_seconds: 60
---

# Hermes and the Fleet IDE

Hermes is MAC's conversational boundary. A tenant contains human users; a
persona supplies identity and memory scope; a Hermes instance runs that persona;
and a platform binding connects the instance to Slack, Telegram, Discord, or
another channel. MAC remains the authority for work and operations.

The following creates the identity graph without contacting an external chat
service.

```bash
mac --db "$DOCS_DB" admin init
mac --db "$DOCS_DB" admin tenant register Tutorial --tenant-id tenant_tutorial
mac --db "$DOCS_DB" admin user register tenant_tutorial reader \
  --user-id user_reader --display-name "Tutorial Reader"
mac --db "$DOCS_DB" admin persona register tenant_tutorial guide \
  --persona-id persona_guide --soul-ref soul://tutorial/guide \
  --memory-scope memory://tutorial/guide
mac --db "$DOCS_DB" admin hermes register tenant_tutorial guide \
  --instance-id hermes_guide --persona-id persona_guide \
  --home-ref home://tutorial/guide
mac --db "$DOCS_DB" admin binding register tenant_tutorial hermes_guide \
  terminal tutorial-session --binding-id binding_tutorial
mac --db "$DOCS_DB" admin hermes context hermes_guide >/dev/null
```

The Fleet IDE is a client of the same hub API. A workstation enrolls with
`mac admin login`, then `make run-gui` uses the active scoped profile. The browser is
not a second control plane and does not carry a private SQLite ledger.

Hermes can create tasks from conversation context, inspect current projects and
work, and write durable memories. It does not silently turn a chat reply into a
completed repository change.
