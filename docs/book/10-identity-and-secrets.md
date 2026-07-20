---
schema: mac.docs.chapter.v1
chapter: 10
title: Identity, Credentials, and Secrets
audiences: [operator, integrator]
timeout_seconds: 60
---

# Identity, Credentials, and Secrets

MAC separates human clients, workers, reviewers, deployers, and global fleet
administrators. Tokens are scoped and revocable. Worker credentials bind to one
agent identity; an agent cannot claim to be a peer by changing a JSON field.

Secrets are encrypted at rest, revealed only through an audited access handle,
and redacted from listings. Values should arrive on standard input or from a
protected file, never as command-line arguments.

```bash
mac --db "$DOCS_DB" init
printf '%s' 'tutorial-value-not-a-real-credential' | \
  mac --db "$DOCS_DB" secret set tutorial-secret --from-stdin \
  --scopes '{"capabilities":["docs"]}' --created-by human
mac --db "$DOCS_DB" secret list | grep '\*\*\*REDACTED\*\*\*' >/dev/null
mac --db "$DOCS_DB" secret audits
mac client enroll --help >/dev/null
```

Fleet Git credentials are projected by source name and recorded only as
secret-free operational learning: host class, operation, outcome, failure
classification, and remediation. Never place raw tokens, authenticated URLs,
or secret-bearing output into task evidence or memory.

Production clients should use `mac login` and secure profiles. The shared admin
token is a recovery authority, not the default credential for every worker.
