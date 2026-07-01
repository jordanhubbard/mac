# SSH Client Bootstrap Contracts

MAC now ships the three security contracts needed by the planned `mac login`
orchestrator:

- `mac fleet ssh-spec` resolves one explicit, secret-free SSH route from
  `~/.mac/fleets.yaml`.
- `mac client enroll|renew|revoke|list` manages independently revocable,
  scoped client principals on the hub.
- `mac client profile ...` atomically installs and selects a portable local
  profile whose bearer token is kept in a separate mode-`0600` credential
  record.

The single-step `mac login`, reconnecting tunnel supervisor, `login status`, and
revoking `logout` commands are still pending. The commands in this document are
the lower-level contract and the supported manual bridge until that
orchestration lands.

## Trust Boundary

Enrollment is a hub-local operation invoked through an SSH session that has
already authenticated the human or automation identity. It is not an HTTP
endpoint and does not accept the shared administrator bearer as a bootstrap
credential.

The hub registry defaults to
`$MAC_HOME/client-principals.json` and can be overridden with
`MAC_CLIENT_PRINCIPALS_FILE`. It contains SHA-256 token hashes, scope metadata,
expiry, and revocation state. Live bearer material is returned once in the
enrollment or renewal manifest. The adjacent
`client-principals.audit.jsonl` records issuance, renewal, rotation, and
revocation without recording the token or its full hash.

The API hot-reloads this registry. Issuance and revocation take effect without
restarting the hub. If a previously active registry becomes unreadable or has
no active principals, authenticated routes fail closed; the static
`MAC_API_TOKEN` remains the recovery authority when configured.

Default client scopes are `read`, `write`, and `dispatch`. `secret`, `deploy`,
or `admin` require the explicit `--allow-elevated` acknowledgement. Two client
IDs always receive different credentials, and renewing one immediately
invalidates its prior token.

## Explicit SSH Route

Put route references, not private key bytes, in the home-scoped fleet registry:

```yaml
fleets:
  production:
    hub_agent: hub
    control_port: 8789
    defaults:
      ssh_jump: ops@bastion.example:2222
      identity_file: ~/.ssh/mac-production
      ssh_known_hosts_file: ~/.ssh/mac-production-known-hosts
      ssh_host_key_policy: strict
    agents:
      - name: hub
        target: mac@hub.internal
        ssh_port: 22
        os: linux
```

Per-agent values override `defaults`. Supported route fields are target/user,
port, `ssh_jump`, `identity_file` or `identity_ref`,
`ssh_known_hosts_file`, `ssh_host_key_fingerprint`, `ssh_host_ca`, and
`ssh_host_key_policy` (`strict`, `accept-new`, or the deliberately explicit
`insecure`). The legacy `ssh_strict_host_key_checking: false` maps to
`accept-new`, not disabled verification.

`ssh_host_ca` is a client-local known-hosts-format file containing
`@cert-authority` entries. It is passed to OpenSSH as `UserKnownHostsFile`; it
must never contain a host CA private key.

Inspect the exact route without exposing key material:

```bash
mac fleet ssh-spec --fleet production --agent hub --portable --json
```

`--portable` rejects a route that depends on an ambient identity or has no
explicit host-key source. Generated SSH and SCP argv include `-F /dev/null`, so
wildcard entries in a developer's `~/.ssh/config` cannot silently change the
route. Fleet token recovery, deploy, soul snapshot, agent migration, and the
Electron bridge consume this same resolver.

## Manual SSH Enrollment

For a hub whose API listens only on `127.0.0.1:8789`, keep a verified tunnel
open in one terminal:

```bash
ssh -N -L 8789:127.0.0.1:8789 \
  -i ~/.ssh/mac-production \
  -o UserKnownHostsFile=~/.ssh/mac-production-known-hosts \
  -o StrictHostKeyChecking=yes \
  -J ops@bastion.example:2222 \
  mac@hub.internal
```

In another terminal, stream the one-time manifest directly into the secure
profile installer so the token is not left in a temporary file:

```bash
ssh -T mac@hub.internal \
  'mac --json client enroll my-laptop \
    --name "My laptop" \
    --fleet production \
    --profile production \
    --api-url http://127.0.0.1:8789 \
    --scopes read,write,dispatch' \
  | mac client profile install -

mac --profile production diagnostics
mac --profile production task stats
mac --profile production agent list
```

Use the same explicit SSH route for both commands in real deployments. The
abbreviated second command above assumes the route is already pinned in an
operator-controlled wrapper or uses the full options from the first command.

The profile is stored as `~/.mac/clients/<profile>.yaml`. It contains no bearer
value, `MAC_SECRET_KEY`, provider credential, Git deploy credential, private
key, SQLite database, or source-checkout path. Its credential reference points
to `~/.mac/credentials/clients/*.token`, which is mode `0600`; both parent
trees are mode `0700`. Normal `profile list` and `profile show` output never
returns the token.

## Renewal And Revocation

Renewal rotates the credential rather than extending the old bearer:

```bash
ssh -T mac@hub.internal \
  'mac --json client renew my-laptop' \
  | mac client profile install - --profile production
```

Revoke on the hub, then remove local state:

```bash
ssh -T mac@hub.internal 'mac client revoke my-laptop'
mac client profile remove production
```

Revoking one principal does not affect any other client or the recovery admin
token.

## Legacy Migration

`mac fleet sync-token` copies the historical shared `MAC_API_TOKEN`. That token
has administrator authority and is a recovery mechanism, not new-client
enrollment.

The bounded migration command can import an existing fleet route and token into
the separated profile layout, but it refuses unless the administrator
authority is acknowledged:

```bash
mac client profile migrate-legacy \
  --fleet production \
  --allow-legacy-admin-token
```

The command is idempotent and creates a mode-`0600` provenance backup on the
first import. The hub now supports scoped client principals, so replace the
migrated admin profile with a scoped SSH enrollment immediately.

## Failure Rules

- Duplicate enrollment refuses by default; use `client renew` or the explicit
  `client enroll --rotate` behavior.
- Registry and credential files with group/world permissions are ignored or
  rejected.
- Unknown scopes and unacknowledged elevated scopes are rejected.
- Profile installation rejects unknown manifest fields, credential-bearing
  URLs, malformed tokens, and strict SSH profiles without pinned host identity.
- Credential writes commit before profile references. Interrupted installation
  therefore leaves the old complete profile active or an unreferenced new
  credential, never a profile pointing at a missing token.
- `mac login` remains absent until it can verify the host fingerprint, choose a
  free local port, supervise/reconnect the tunnel, validate an authenticated
  request, and roll back all partial local state as one command.
