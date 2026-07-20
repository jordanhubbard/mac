# SSH Client Bootstrap Contracts

MAC ships the complete SSH-first login orchestration plus its three lower-level
security contracts:

- `mac fleet ssh-spec` resolves one explicit, secret-free SSH route from
  `~/.mac/fleets.yaml`.
- `mac client enroll|renew|revoke|list` manages independently revocable,
  scoped client principals on the hub.
- `mac client profile ...` atomically installs and selects a portable local
  profile whose bearer token is kept in a separate mode-`0600` credential
  record.
- `mac login`, `mac login status`, `mac login renew`, and `mac logout --revoke`
  compose those primitives into a verified, recoverable client lifecycle.

## Managed Login

For an explicit hub route:

```console
mac login --ssh mac@hub.internal \
  --identity-file ~/.ssh/mac-production \
  --known-hosts-file ~/.ssh/mac-production-known-hosts \
  --fleet production --profile production --client-id my-laptop
```

Or consume the canonical route in `~/.mac/fleets.yaml`:

```console
mac login --fleet production --profile production --client-id my-laptop
```

Identity and host trust are validated when supplied and otherwise fall back to
OpenSSH's own resolution, so `mac login --ssh <host>` behaves like `ssh <host>`:

- **Identity** — an explicit `--identity-file` (or a fleet `identity_file`) is
  validated (must exist, `chmod 600`) and pinned with `IdentitiesOnly`. When
  none is given, ssh selects its default identities and the agent.
- **Host trust** — an explicit `--known-hosts-file`, `--host-ca`, or
  `--host-key-fingerprint` pins the host under strict checking. A directly
  reachable host pinned by fingerprint is scanned with `ssh-keyscan`, and only
  the matching key is retained in a private profile-owned known-hosts file
  (fingerprint discovery is refused for a ProxyJump route, since an
  unauthenticated scan cannot authenticate both hops — provide a verified
  known-hosts file instead). When no trust material is given, login verifies
  against the operator's default `~/.ssh/known_hosts` using `accept-new`
  (trust-on-first-use), matching interactive ssh rather than failing.

For a reproducible, portable client profile — one that does not depend on the
enrolling machine's ambient ssh state — supply both an explicit identity and
explicit host trust (or configure them on the fleet's hub agent). Exported
profiles record exactly what was resolved.

The command reserves a free loopback port and starts SSH with `-F /dev/null`,
`BatchMode=yes`, `ExitOnForwardFailure=yes`, strict host checking, server-alive
probes, and the explicit route. It invokes scoped enrollment in a second SSH
session, running the hub's `mac` executable. Because a non-interactive SSH
command shell does not source the operator's login profile, the remote `mac`
path is discovered automatically (well-known install locations plus the shell's
own `command -v`); pass `--remote-mac <abs path>` only to override that. It then
validates `GET /tasks/stats` with the returned bearer through the
tunnel, writes secret-free session state, and atomically installs the profile
only after validation succeeds. Any failure after issuance attempts remote
revocation and removes transient state without printing the bearer.

After login, operator commands omit `--db` and resolve the installed profile as
the one hub authority. MAC no longer creates a client-side database
implicitly. A legacy `~/.mac/mac.db` is neither a cache nor an
offline queue, and MAC performs no implicit task reconciliation between SQLite
files. If that path already contains tasks, task-producing commands refuse to
add more unless `--local-authority` explicitly declares that the file is the
database used by a standalone API, dispatcher, and worker set.

Successful login and login-status output include a `local_ledger` notice when
that database contains active work. The notice is read-only and points to
`mac migrate local-ledger`; migration remains an explicit `--execute` action
that verifies hub copies before cancelling and archiving local records.

The same rule applies to deployed fleet spokes. They do not run a local
control-plane service and their generated environment contains no database
setting. Workers, fleet-context refresh, and Hermes identity registration all
use the hub API.

The managed SSH PID is recorded under `$MAC_HOME/sessions`; bearer material is
not. Every profile-backed CLI command checks the session. A dead tunnel is
restarted from the pinned profile and its bearer is validated before the
requested API call proceeds. `mac login status` is read-only and reports a dead,
unreachable, or rejected session without exposing the token.

```console
mac login status --profile production
mac login renew --profile production
mac logout --profile production --revoke
```

Renewal rotates rather than extends the bearer. Revoking logout performs the
hub revocation first; if SSH revocation fails, local credential state is kept so
the operator can retry. Plain `mac logout` is deliberately local-only and leaves
the remote principal active.

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

```console
mac fleet ssh-spec --fleet production --agent hub --portable --json
```

`--portable` rejects a route that depends on an ambient identity or has no
explicit host-key source. Generated SSH and SCP argv include `-F /dev/null`, so
wildcard entries in a developer's `~/.ssh/config` cannot silently change the
route. Fleet token recovery, deploy, soul snapshot, agent migration, and the
Electron bridge consume this same resolver.

## Manual SSH Enrollment (Recovery)

For a hub whose API listens only on `127.0.0.1:8789`, keep a verified tunnel
open in one terminal:

```console
ssh -N -L 8789:127.0.0.1:8789 \
  -i ~/.ssh/mac-production \
  -o UserKnownHostsFile=~/.ssh/mac-production-known-hosts \
  -o StrictHostKeyChecking=yes \
  -J ops@bastion.example:2222 \
  mac@hub.internal
```

In another terminal, stream the one-time manifest directly into the secure
profile installer so the token is not left in a temporary file:

```console
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

```console
ssh -T mac@hub.internal \
  'mac --json client renew my-laptop' \
  | mac client profile install - --profile production
```

Revoke on the hub, then remove local state:

```console
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

```console
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
- Managed login refuses an occupied requested port before enrollment. A stale
  session is restarted only when the recorded PID is still identifiable as the
  expected SSH forward; unrelated processes are never killed.
- The token is never placed in SSH argv, local session state, normal/JSON
  output, or error text. It exists in memory until API validation and then only
  in the mode-`0600` credential record.
