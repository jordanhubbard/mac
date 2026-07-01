# Secrets Management Guide

This guide covers the full lifecycle of fleet secrets: creation, scoping,
auditing, revealing, and rotating. It applies to any fleet running the MAC
control plane.

---

## Overview

Secrets are durable, encrypted credentials stored in the control-plane
database. Each secret:

- Is encrypted at rest with Fernet symmetric encryption (key derived from
  `MAC_SECRET_KEY` via HKDF).
- Is access-gated by a **scope** that names who can request it (by agent ID,
  capability tag, or tenant membership).
- Is audited on every request — granted or denied — so every access is
  traceable in the audit trail.
- Can only be revealed once per request via a single-use, time-limited
  **handle**.

---

## Lifecycle: set → scope → audit → reveal → rotate

### 1. Set a Secret

Create a secret with a name, plaintext value, and a scope dict that controls
who can request it:

```python
from mac.services import ControlPlane

cp = ControlPlane.in_memory()  # use your real store in production

# Scope by agent ID — only worker-1 can request this secret
secret = cp.secrets.create_secret(
    name="deploy-credential",
    value="<plaintext-deploy-token>",
    scopes={"agents": ["agent_<worker-1-id>"]},
    created_by="hub",
)
print(secret.id)   # e.g. secret_abc123
```

The plaintext is encrypted immediately; `SecretRecord.to_dict()` always
returns `"***REDACTED***"` in the `value` field so logs and API responses
never expose the real value.

### 2. Define a Scope

A scope dict controls which agents can access the secret. Three scope types
can be combined:

| Scope key      | Meaning |
|----------------|---------|
| `agents`       | List of agent IDs that may request the secret |
| `capabilities` | List of capability tags; any agent with a matching capability is allowed |
| `tenant_ids`   | List of tenant IDs; the requesting agent's machine must be tenanted to one of these |

All conditions within a single scope are OR-combined; but if `tenant_ids`
is present the requesting agent's machine must satisfy that tenant restriction
first before the agent/capability check applies.

Examples:

```python
# Only a specific agent
scopes = {"agents": ["agent_<worker-1-id>"]}

# Any agent with the 'deploy' capability
scopes = {"capabilities": ["deploy"]}

# Any agent tagged 'admin' or 'dispatch'
scopes = {"capabilities": ["admin", "dispatch"]}

# Any agent in tenant-A, regardless of capability
scopes = {"tenant_ids": ["tenant_<alpha-id>"]}

# Specific agent AND the machine must be in tenant-A
scopes = {"agents": ["agent_<worker-1-id>"], "tenant_ids": ["tenant_<alpha-id>"]}
```

### 3. Request a Handle

An agent requests a time-limited, single-use handle. The control plane checks
the scope, machine trust, and enabled flag, then writes an audit record before
returning the handle:

```python
handle = cp.secrets.request_secret(
    secret_id_or_name="deploy-credential",
    accessor_agent_id="agent_<worker-1-id>",
    purpose="ci-deploy",
    ttl_seconds=300,   # handle expires in 5 minutes
)
# handle.granted == True  →  proceed to reveal
# handle.granted == False →  AuthorizationError was raised before this line
```

If the scope or machine trust check fails, `AuthorizationError` is raised and
the denial is written to the audit trail.

### 4. Reveal the Secret

Use the handle to retrieve the plaintext. This marks the handle as spent:

```python
plaintext = cp.secrets.reveal_secret(
    secret_id=handle.secret_id,
    audit_id=handle.audit_id,
    accessor_agent_id="agent_<worker-1-id>",
)
# Use plaintext once, then discard — the same handle cannot be redeemed again.
```

A second call with the same `audit_id` raises `AuthorizationError`.

### 5. Rotate the Secret

Replace the ciphertext with a new value without changing the secret ID or name.
A `ROTATED` audit record is written automatically:

```python
updated = cp.secrets.rotate_secret(
    secret_id_or_name="deploy-credential",
    value="<new-plaintext-deploy-token>",
    actor="hub",
)
print(updated.rotated_at)  # ISO timestamp of the rotation
```

---

## Scoped-Token Authorization Patterns

### Agent-API-Key Pattern

An agent that needs its own API key to call an upstream service should store
the key scoped to its own agent ID:

```python
cp.secrets.create_secret(
    "worker-1-api-key",
    "<api-key>",
    {"agents": ["agent_<worker-1-id>"]},
    "hub",
)
```

No other agent can request this key even if they know the name.

### Shared Deploy Credential Pattern

Multiple deploy agents share a credential by scoping via capability:

```python
cp.secrets.create_secret(
    "registry-push-token",
    "<push-token>",
    {"capabilities": ["deploy", "image-builder"]},
    "hub",
)
```

Any new deploy agent automatically gains access when it's registered with the
`deploy` or `image-builder` capability.

### Client Principal And Profile Pattern

New control clients must not receive the shared `MAC_API_TOKEN`. On the hub,
`mac client enroll` creates a distinct scoped bearer and stores only its
`sha256:` hash in `$MAC_CLIENT_PRINCIPALS_FILE` (default
`$MAC_HOME/client-principals.json`). The adjacent audit JSONL contains client
ID, scopes, version, actor, and lifecycle event, but no token or full stored
hash. Both files are mode `0600` under a mode-`0700` MAC home.

The default scopes are `read`, `write`, and `dispatch`. `secret`, `deploy`, and
`admin` issuance fails unless the hub operator passes `--allow-elevated`.
Renewal rotates the bearer and immediately invalidates its predecessor;
revocation affects only that client. The API reads the hashed registry on each
file change, so neither action needs a service restart.

Stream the one-time JSON enrollment output directly into
`mac client profile install -`. The local profile YAML holds connection,
host-key, scope, expiry, and credential-reference metadata only. Its bearer is
stored separately under `~/.mac/credentials/clients/` with mode `0600` and is
redacted from normal profile output. Unknown manifest fields, credential-bearing
URLs, and strict SSH profiles without pinned host identity are rejected.

`mac fleet sync-token` remains an administrator-token recovery command for an
existing operator workstation. It is not a client issuance mechanism. The
bounded `client profile migrate-legacy` command requires
`--allow-legacy-admin-token`, makes a secure first-import backup, and labels the
resulting authority accurately as `admin` until it is replaced with scoped
enrollment.

See [SSH Client Bootstrap Contracts](client-bootstrap-contract.md) for the
manual SSH workflow and failure rules while the single-step `mac login`
orchestrator is pending.

### Git-Host Credential Pattern

Repository credentials have two distinct layers:

1. The encrypted MAC secret record controls durable storage and audited reveal.
2. The worker or Kubernetes Job environment supplies the host-specific variable
   that Git operations actually resolve.

Creating a vault secret named `github.token` does **not** automatically place it
in a worker process. Fleet deploy accepts `MAC_DEPLOY_GH_TOKEN` from the
operator's host-local `~/.mac/.env` and writes it to managed runtimes as
`GH_TOKEN`. Kubernetes task and review Jobs read optional `GH_TOKEN`,
`GITHUB_TOKEN`, and `GITEA_TOKEN` keys from the runner's configured Secret
(default `mac-api-config`). Keep Git tokens out of `fleets.yaml`, committed
fleet specs, task metadata, and operator CLI arguments. The internal resolver
may place a credential in the argv of the one Git subprocess that needs it;
process inspection on an execution host is therefore privileged access.

At runtime, GitHub HTTPS access resolves `GH_TOKEN`, then `GITHUB_TOKEN`, then
the host-mode `MAC_TASK_GIT_TOKEN` fallback. Gitea resolves `GITEA_TOKEN`, then
the fallback. The credential is injected only into the individual Git command;
the worker restores a credential-free `origin` immediately afterward. Review
Jobs receive the optional Git-host keys but deliberately do not receive
`MAC_SECRET_KEY`.

Repository access writes `fleet_learning:repository_access` common-memory
records so reviewer routing can reuse proven success and avoid a recent auth
failure. Those records contain only the credential source *name* (for example
`env:GH_TOKEN`), never its value. They are operational routing data, not a
replacement secret store. See [Fleet Operational Learning](fleet-operational-learning.md).

### Tenant-Isolated Secret Pattern

A credential that belongs to one tenant's workload should be scoped to that
tenant so no agent from a different tenant can ever request it:

```python
cp.secrets.create_secret(
    "tenant-alpha-db-password",
    "<password>",
    {"tenant_ids": ["tenant_<alpha-id>"]},
    "hub",
)
```

Agents whose machines are tagged with a different tenant policy will always
receive `AuthorizationError`.

---

## Audit-Trail Interpretation

Every `request_secret`, `reveal_secret`, `rotate_secret`, and
`resolve_secret_value` call writes a row to `secret_access_audit`.

```python
audits = cp.secrets.list_audits(secret_id="secret_<id>")
for a in audits:
    print(a.result, a.accessor_agent_id, a.purpose, a.revealed_at)
```

| `result` field | Meaning |
|----------------|---------|
| `granted`      | Scope check passed; handle was issued |
| `denied`       | Scope or trust check failed; no handle issued |
| `rotated`      | Secret value was rotated; `actor` is recorded in `accessor_agent_id` |

Key fields:

- `created_at` — when the request was processed
- `expires_at` — when the handle expires (only set for `granted` rows)
- `revealed_at` — when the plaintext was revealed (non-null = the handle was redeemed)
- `purpose` — free-text description provided by the caller (e.g. `"ci-deploy"`)

A `granted` row with `revealed_at IS NULL` means a handle was issued but never
redeemed — either the TTL expired or the agent didn't follow through.

---

## Worked Examples

### Example A: Slack Bot Token

```python
# 1. Set the secret, scoped to the messaging agent by capability
cp.secrets.create_secret(
    "slack-bot-token",
    "xoxb-<actual-token>",
    {"capabilities": ["messaging"]},
    "hub",
)

# 2. Messaging agent requests a handle before sending a message
handle = cp.secrets.request_secret(
    "slack-bot-token",
    accessor_agent_id="agent_<messaging-agent-id>",
    purpose="send-notification",
)

# 3. Agent reveals and uses the token
token = cp.secrets.reveal_secret(
    handle.secret_id, handle.audit_id, "agent_<messaging-agent-id>"
)
# use token to call Slack API ...
```

### Example B: Deploy Credential

```python
# 1. Store during fleet setup
cp.secrets.create_secret(
    "registry-deploy-key",
    "<deploy-key>",
    {"capabilities": ["deploy"]},
    "hub",
)

# 2. Deploy agent requests a handle before pushing an image
handle = cp.secrets.request_secret(
    "registry-deploy-key",
    accessor_agent_id="agent_<worker-1-id>",
    purpose="image-push",
    ttl_seconds=120,
)
cred = cp.secrets.reveal_secret(
    handle.secret_id, handle.audit_id, "agent_<worker-1-id>"
)
# run docker push with cred ...

# 3. Rotate after a security event
cp.secrets.rotate_secret("registry-deploy-key", "<new-deploy-key>", actor="hub")
```

### Example C: Agent API Key for an Upstream LLM Provider

```python
# 1. Operator sets the key during provisioning
cp.secrets.create_secret(
    "openai-api-key",
    "sk-<actual-key>",
    {"capabilities": ["llm-caller"]},
    "hub",
)

# 2. The model router resolves it in-process (no handle dance)
value = cp.secrets.resolve_secret_value("openai-api-key", purpose="router", accessor="router")
# value is None if the secret is absent or disabled — fallback to env
```

`resolve_secret_value` is intended for control-plane-internal callers (e.g.
the model router on the hub) that decrypt a secret they already own. It
still emits an audit record but skips the request/reveal two-step.

---

## Disabling and Deleting Secrets

Disable a secret (leaves audit trail intact):

```python
cp.store.execute("UPDATE secrets SET enabled = 0 WHERE id = ?", (secret.id,))
# All future request_secret calls will raise AuthorizationError.
# resolve_secret_value returns None instead of raising.
```

Hard-delete a secret (removes ciphertext and row; audit rows cascade away):

```python
result = cp.secrets.delete_secret("deploy-credential", actor="hub")
# result["deleted"] == True
```

Prefer disabling over deleting when you want to keep the audit history and
allow the name to be reclaimed later.

---

## Security Notes

- **Untrusted machines**: agents on machines with `trusted = False` are always
  denied, regardless of scope. Ensure machines are marked trusted only after
  fleet validation.
- **Single-use handles**: each `request_secret` produces exactly one handle.
  A single agent cannot reuse the same handle to reveal the secret twice.
- **TTL enforcement**: handles expire after `ttl_seconds` (default 300 s).
  Expired handles raise `AuthorizationError` on reveal.
- **Audit on denial**: every denied request is logged; monitor for repeated
  denials as a signal of a misconfigured scope or a credential-harvesting
  attempt.
- **Key rotation**: `MAC_SECRET_KEY` is used to derive the Fernet key. If the
  environment key is rotated, all existing ciphertext becomes unreadable until
  the secrets are re-encrypted. Plan key rotation carefully.
- **Operational memory is not a secret store**: repository-access learnings may
  name `env:GH_TOKEN` or another mechanism, but must never contain token values,
  authenticated URLs, or raw secret-bearing Git output.
