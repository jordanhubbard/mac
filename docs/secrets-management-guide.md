# Secrets Management Guide

This guide explains how to create, scope, audit, reveal, and rotate secrets
in MAC, and how scoped-token authorization lets agents access credentials with
a full, tamper-evident audit trail.

---

## Table of Contents

1. [Concepts](#1-concepts)
2. [Lifecycle: set -> scope -> audit -> reveal -> rotate](#2-lifecycle)
3. [Scoped-Token Authorization Patterns](#3-scoped-token-authorization-patterns)
4. [Audit-Trail Interpretation](#4-audit-trail-interpretation)
5. [Worked Examples](#5-worked-examples)
6. [CLI Quick Reference](#6-cli-quick-reference)
7. [Security Properties and Guarantees](#7-security-properties-and-guarantees)

---

## 1. Concepts

### Secret

A named, encrypted credential stored in the MAC control plane.  Secrets never
leave storage in plaintext: they are encrypted with Fernet (AES-128-CBC +
HMAC-SHA256) using a key derived from `MAC_SECRET_KEY` via HKDF.

A **SecretRecord** has:

| Field | Description |
|---|---|
| `id` | Stable unique identifier (`secret_…`) |
| `name` | Human-readable, mutable label (e.g. `slack-token`) |
| `scopes` | JSON object controlling who may access it (see below) |
| `created_by` | Principal who created the secret |
| `enabled` | Boolean kill-switch; when false all requests are denied |
| `rotated_at` | Timestamp of last rotation (null until first rotation) |

The plaintext value is **never** returned by `get_secret`, `list_secrets`, or
their API/CLI equivalents.  `to_dict()` always substitutes `"***REDACTED***"`.

### Scope

Scopes are a JSON object that declares which agents are allowed to request a
secret.  The matching logic is:

1. **Tenant gate** (if `tenant_ids` or `tenant_id` is present): the requesting
   agent's machine must satisfy at least one of the listed tenant IDs under its
   `tenant_policy` label.  If the tenant gate fails the request is denied
   regardless of agents/capabilities lists.
2. **Agent-id match**: if the agent's own id appears in `scopes.agents`, grant.
3. **Capability intersection**: if `scopes.capabilities` is non-empty and the
   agent's capabilities include at least one entry, grant.
4. **Tenant-only scope**: if the secret scopes exclusively by tenant (no agents
   or capabilities list), passing the tenant gate is sufficient — grant.

If none of the above is satisfied, deny.

### SecretHandle

When `request_secret` succeeds it returns a **SecretHandle**:

```
{
  "secret_id": "secret_abc123",
  "audit_id":  "audit_xyz789",
  "handle":    "secret://secret_abc123#audit_xyz789",
  "granted":   true
}
```

The handle URI encodes both the secret and the specific audit row.  The agent
passes both IDs to `reveal_secret` to receive the plaintext — **once**.
Handles are single-use and time-limited (default 300 seconds).

### Audit Entry (SecretAccess)

Every access attempt — granted or denied — creates an immutable row in
`secret_access_audit`:

| Field | Description |
|---|---|
| `id` | Unique audit id (`audit_…`) |
| `secret_id` | Which secret was accessed |
| `accessor_agent_id` | Which agent made the request |
| `purpose` | Human-readable reason supplied by the requestor |
| `result` | `granted`, `denied`, or `rotated` |
| `expires_at` | When the handle expires (null for non-granted rows) |
| `revealed_at` | When the agent actually called `reveal_secret` (null until revealed) |
| `created_at` | When this audit entry was created |

---

## 2. Lifecycle

### Step 1 — Set a secret

```bash
mac secret set <name> --value "<plaintext>" \
    --scope '{"agents": ["agent_deploy_01"]}'
```

Or via the Python API:

```python
rec = cp.create_secret(
    name="slack-webhook",
    value="https://hooks.slack.com/services/…",
    scopes={"capabilities": ["notify"]},
    created_by="operator",
)
print(rec.id)   # secret_…
```

The plaintext is encrypted immediately; the ciphertext is persisted, the
plaintext is discarded.

### Step 2 — Scope the secret

Scopes are set at creation time and can be updated with `rotate_secret` if you
need to change the value at the same time, or directly via the API/CLI.

Common scope patterns:

```jsonc
// Single agent
{"agents": ["agent_deploy_01"]}

// Any agent with a specific capability
{"capabilities": ["deploy"]}

// Tenant-gated (any agent whose machine allows tenant 'acme')
{"tenant_ids": ["acme"]}

// Combined: tenant gate AND capability check
{"tenant_ids": ["acme"], "capabilities": ["admin"]}
```

### Step 3 — Audit: inspect who has access

```bash
mac secret audit <name-or-id>
```

This lists all `secret_access_audit` rows for that secret, showing every
attempt, result, and whether the handle was revealed.

### Step 4 — Reveal (agent side)

Agents do not call the secrets service directly.  The normal flow is:

1. Agent calls `request_secret(secret_id_or_name, agent_id, purpose)`.
2. The service checks scopes + machine trust.  On success a `SecretHandle` is returned.
3. Agent calls `reveal_secret(secret_id, audit_id, agent_id)`.
4. The service atomically marks the audit row as revealed and returns plaintext.

The plaintext is never persisted after reveal.  If the agent needs the value
again it must call `request_secret` again (a new audit row is created each
time).

```python
handle = cp.request_secret("slack-webhook", "agent_deploy_01", "post-deploy-notification")
plaintext = cp.reveal_secret(handle.secret_id, handle.audit_id, "agent_deploy_01")
```

### Step 5 — Rotate

Rotation replaces the ciphertext with a new encryption of a new plaintext.
Existing unredeemed handles become invalid after rotation because the new
ciphertext is returned regardless of which handle generation the audit row
belongs to.  A `rotated` audit entry is written automatically.

```bash
mac secret rotate <name-or-id> --value "<new-plaintext>"
```

```python
rec = cp.rotate_secret("slack-webhook", new_value="https://…/new-token", actor="operator")
print(rec.rotated_at)  # timestamp of rotation
```

---

## 3. Scoped-Token Authorization Patterns

### Pattern A — Agent-id allowlist

Use when a single, known agent owns a credential (e.g. a deploy bot).

```json
{"agents": ["agent_deploy_prod_01"]}
```

Grants access only to that exact agent.  Any other agent — even on the same
machine — is denied.

### Pattern B — Capability-based delegation

Use when a class of agents should share access (e.g. all agents with the
`secret` capability) and you do not want to maintain an explicit list.

```json
{"capabilities": ["secret"]}
```

Any agent whose `capabilities` array contains `"secret"` is granted access,
provided its machine is trusted.

### Pattern C — Tenant isolation

Use when you run multiple tenants on a shared fleet and want each tenant's
secrets to be invisible to others.

```json
{"tenant_ids": ["acme-corp"]}
```

The `tenant_policy` label on the machine determines which tenant(s) the machine
serves.  A machine labelled `{"mode": "private", "tenant_ids": ["acme-corp"]}`
will pass the tenant gate for `"acme-corp"` and fail for all others.

### Pattern D — Tenant + capability

Combine tenant scoping with a capability check for the tightest control:
the agent's machine must serve the right tenant **and** the agent must have
the required capability.

```json
{"tenant_ids": ["acme-corp"], "capabilities": ["admin"]}
```

### Pattern E — Hub-side resolution (no handle dance)

Services co-located with the control plane (e.g. the model router) can call
`resolve_secret_value(name)` directly.  This bypasses the request/reveal
handle dance because the control plane already owns the Fernet key.  The call
is still audited with result `granted` and purpose `"router"` by default.

```python
api_key = cp.secrets.resolve_secret_value("openai-key", purpose="model-router")
```

---

## 4. Audit-Trail Interpretation

Every `secret_access_audit` row tells a complete story.  Common read patterns:

### Who has accessed a secret?

```python
audits = cp.list_secret_audits(secret_id="secret_abc123")
for a in audits:
    print(a.accessor_agent_id, a.result, a.purpose, a.created_at)
```

### Was the handle actually redeemed?

Check `revealed_at`.  If `null`, the agent received a handle but never called
`reveal_secret` (possibly a cancelled job or an auth error downstream).

### Are there denied attempts?

```python
denied = [a for a in audits if a.result == "denied"]
```

Repeated denials from an unexpected agent ID may indicate a misconfigured scope
or a probing attempt.

### Rotation history

Rotation events appear as `result = "rotated"` with `purpose = "rotate"` and
`accessor_agent_id` set to the actor string passed to `rotate_secret`.

### Reading the results

| `result` | Meaning |
|---|---|
| `granted` | Access allowed; handle issued |
| `denied` | Access refused (scope mismatch, untrusted machine, disabled) |
| `rotated` | Secret value was replaced by this actor |

---

## 5. Worked Examples

### Example A — Slack webhook token

**Goal:** A notification agent needs to post to Slack after a deploy.

```bash
# Operator stores the token
mac secret set slack-deploy-webhook \
    --value "https://hooks.slack.com/services/T00/B00/…" \
    --scope '{"capabilities": ["notify"]}'

# Agent (agent_notifier_01) requests and reveals the token
handle=$(mac secret request slack-deploy-webhook agent_notifier_01 "post-deploy")
mac secret reveal $handle agent_notifier_01
```

```python
# Python equivalent
handle = cp.request_secret("slack-deploy-webhook", "agent_notifier_01", "post-deploy")
webhook_url = cp.reveal_secret(handle.secret_id, handle.audit_id, "agent_notifier_01")
requests.post(webhook_url, json={"text": "Deploy succeeded!"})
```

Audit trail shows:
- One `granted` entry for `agent_notifier_01` at deploy time.
- `revealed_at` is set once the agent redeems the handle.

### Example B — Deploy credential (scoped to a single agent)

**Goal:** A specific deploy agent needs SSH keys stored as a secret.

```bash
mac secret set deploy-ssh-key \
    --value "$(cat ~/.ssh/id_deploy_rsa)" \
    --scope '{"agents": ["agent_deploy_prod"]}'
```

Only `agent_deploy_prod` can request this secret.  Any other agent — even with
admin capabilities — is denied.

### Example C — Agent API key (tenant-scoped)

**Goal:** Each tenant has its own downstream API key; agents should only see
their tenant's key.

```bash
# Store per-tenant key
mac secret set acme-api-key \
    --value "sk-acme-…" \
    --scope '{"tenant_ids": ["acme"]}'

mac secret set beta-api-key \
    --value "sk-beta-…" \
    --scope '{"tenant_ids": ["beta"]}'
```

Machines are labelled with their tenant policy:
```json
{"tenant_policy": {"mode": "private", "tenant_ids": ["acme"]}}
```

An agent on the `acme` machine can access `acme-api-key` but will be denied
`beta-api-key`.  The denial is recorded in the audit trail.

### Example D — Rotation after credential leak

```bash
# Immediately disable the old secret
mac secret set leaked-token --value "<old>" --scope '{"agents": ["agent_A"]}'
# … discovered leaked …

# Rotate to new value
mac secret rotate leaked-token --value "<new>"

# Verify rotation in audit trail
mac secret audit leaked-token
# Shows: result=rotated, purpose=rotate, actor=operator
```

Any unredeemed handles from before the rotation are invalidated because
`reveal_secret` re-fetches the ciphertext from the DB; the new ciphertext
decrypts to the new value.  Old handles that were already revealed (single-use)
are already consumed.

---

## 6. CLI Quick Reference

```bash
# Create a secret
mac secret set <name> --value "<plaintext>" --scope '<json>'

# List all secrets (plaintext is redacted)
mac secret list

# Show a single secret (no plaintext)
mac secret show <name-or-id>

# Request a handle (agent-side)
mac secret request <name-or-id> <agent-id> "<purpose>"

# Reveal using a handle
mac secret reveal <secret-id> <audit-id> <agent-id>

# Rotate value
mac secret rotate <name-or-id> --value "<new-plaintext>"

# Delete permanently
mac secret delete <name-or-id>

# Audit trail
mac secret audit <name-or-id>          # for one secret
mac secret audit                        # all secrets
```

---

## 7. Security Properties and Guarantees

| Property | How it is enforced |
|---|---|
| **Plaintext never persisted** | `create_secret`/`rotate_secret` encrypt before writing; `get_secret`/`list_secrets` never return ciphertext to callers |
| **Encryption at rest** | Fernet (AES-128-CBC + HMAC-SHA256); key derived via HKDF from `MAC_SECRET_KEY` with a fixed salt — the stored bytes cannot be decrypted without the same secret key |
| **Single-use handles** | `reveal_secret` atomically sets `revealed_at`; a second call on the same audit row is rejected |
| **Time-limited handles** | `expires_at` is checked atomically during reveal; default TTL is 300 seconds |
| **Machine trust gate** | Any agent on an untrusted machine is denied regardless of scope |
| **Tenant isolation** | Tenant-scoped secrets enforce `machine.labels.tenant_policy`; an agent on a foreign-tenant machine cannot access cross-tenant secrets |
| **Immutable audit trail** | Every access attempt writes to `secret_access_audit`; rows are never updated (except `revealed_at` set once on reveal) |
| **No placeholder keys** | `ControlPlane.__init__` rejects well-known placeholder substrings (`REPLACE-ME`, `CHANGE-ME`, etc.) so the example env file cannot be deployed verbatim |
| **Cascade on delete** | Deleting a secret removes its audit rows via `ON DELETE CASCADE`; stale audit rows referencing a gone secret cannot accumulate |

### Threat model notes

- The control plane must be deployed with a strong, unique `MAC_SECRET_KEY`
  (generate with `openssl rand -base64 48`).
- Rotating `MAC_SECRET_KEY` requires re-encrypting all stored ciphertexts;
  there is currently no built-in migration path — plan key rotation carefully.
- `resolve_secret_value` is intentionally restricted to co-located callers
  (the hub process).  Do not expose this method over an API endpoint.
- Audit rows are retained until the parent secret is deleted.  For compliance
  workflows, export audit rows before deleting a secret.
