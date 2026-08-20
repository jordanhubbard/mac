# Hub repair key

The hub generates an SSH keypair of its own and every worker authorizes it
during bootstrap, so a wedged worker stays reachable when the operator who
provisioned it is not.

## The gap this closes

A bootstrapped fleet has two kinds of hub-to-worker connectivity, and neither
one can repair anything:

* **The control plane.** The hub and the worker talk continuously over HTTP.
  That link carries tasks, leases, and heartbeats. It runs *inside* the agent,
  so when the agent is the thing that is wedged, the link is exactly as wedged.
* **The reverse tunnel.** The hub already owns `~/.ssh/mac_tunnel_id` and
  spokes already authorize it (`install_hub_tunnel_pubkey` in
  `deploy/fleet-node-install.sh`). But that key exists to move forwarded ports —
  the hub uses it as `ssh -N -R …` and never runs a command with it.

Everything that can actually fix a node — restart a supervisor, look at a
deploy log, find out which revision is deployed — went through the person who
provisioned it, over their point-to-point SSH access. That is a single point of
failure with a human in it: rotated or expired credentials, a laptop that is
offline, a different operator running the fleet six months later.

The hub therefore gets a key of its own. It is added **alongside** the
provisioner's key in each worker's `authorized_keys`, never instead of it; the
operator path is unchanged and remains the way to do anything outside the repair
verb set.

## What the key may do

Not open a shell. The worker's entry looks like this:

```text
restrict,command="/home/mac/.mac/bin/mac-hub-repair" ssh-ed25519 AAAA… mac-hub-repair
```

`restrict` withdraws pty allocation, agent forwarding, port forwarding, X11 and
user rc in one option, so the entry cannot be accidentally widened later by
forgetting a new `no-*` flag when OpenSSH grows a new capability. The forced
`command=` then narrows the single remaining capability to a generated shim,
which accepts one verb from a closed allowlist and refuses everything else —
including an empty request, which is what an attempt at an interactive shell
looks like from the far side.

| Verb | Arguments | What it does |
| --- | --- | --- |
| `status` | — | agent, host, supervisor, deployed revision, service states |
| `services` | — | supervisor state of each mac-managed service |
| `restart` | `<service>` | restart one allowlisted service (`mac`, `hermes`, `agent`) |
| `logs` | — | list the log files available under `~/.mac/logs` |
| `tail` | `<log> [lines]` | tail one of those files, bounded to 500 lines |
| `deploy-info` | — | deployed source revision and deploy generation |

Requests are tokenized with globbing off and every token is checked against
`[A-Za-z0-9._:@-]` before anything is dispatched. The alphabet has no `/`, no
quotes, and no shell metacharacters, so path traversal and command chaining are
absent from the grammar rather than filtered out of it afterwards. Every
request — allowed or denied — is appended to `~/.mac/logs/hub-repair.log` with
the peer address, and a denial is recorded as denied even when the verb itself
was valid but its argument was not.

A denied request exits `78` (`EX_CONFIG`), so "the hub was refused" never reads
as "the repair ran and failed".

The shim is dependency-free POSIX `sh`: no Python, no venv, no deployed source
tree. That is deliberate. The repair path has to keep working on the node whose
venv is half-installed or whose source was rolled back, because that is the node
someone needs to reach. Everything variable — the supervisor kind and the
service allowlist — is baked in at install time by the deploy, which already
knows those values.

## Where the private key lives

`~/.mac/keys/mac-hub-repair-id` on the hub node, mode `0600` in a `0700`
directory.

`~/.mac` is node state that a deploy preserves: a deploy replaces `~/.mac/src`
and `~/.mac/venv` (backing up the previous ones) and leaves the rest alone. It
is also never read into a release archive, so the key survives redeploys without
ever becoming a deploy artifact. A key under the deployed source tree would fail
both ways at once — destroyed by the next deploy, and shipped by the one after.

## Rotation

The key is created if absent and **not** rotated on redeploy.

Rotating on every deploy sounds safer and is not: a hub key that changed last
Tuesday is stale on precisely the workers that missed last Tuesday's deploy,
which is the population you need to reach. Generation is create-if-absent;
rotation is an explicit operator act (remove the keypair, deploy the hub, then
deploy the workers).

What makes rotation safe is the merge. `mac.hub_repair_key.merge_authorized_keys`
replaces the previous hub entry instead of appending next to it: an entry is
recognized as the hub's by its `mac-hub-repair` comment marker or by carrying
the same key material under any other comment. Everything else in the file — the
provisioner's key above all — is copied through byte for byte and keeps its
position. Contrast the older `grep -qF … || printf >>` pattern used for the
tunnel key, under which a rotated key leaves its predecessor authorized forever.

## ProxyJump and the `ssh_jump` bastion

Unchanged and reused. A deploy installs the fleet registry at
`~/.mac/fleets.yaml` on every node, so the hub resolves a worker route through
`mac.fleet_ssh` exactly as an operator's client does, inheriting the fleet's
`ssh_jump` bastion, port, and host-key policy. For an in-cluster pod that means
the hub reaches the worker through the same bastion the deploy uses. The hub
needs its own *key*, not its own *transport*:

```console
python3 -m mac.hub_repair_key ssh-argv --agent gke-worker-1 restart mac
```

resolves the registry route, swaps in the hub's repair identity, and prints the
`ssh` argv — `ProxyJump` and all.

## Rollout

The two halves land in separate deploys, and that is fine:

1. **Deploy the hub.** `ensure_hub_repair_key` creates the keypair.
2. **Deploy the workers.** The cohort reads the hub's public key once
   (`read_hub_repair_pubkey`), ships it as `MAC_DEPLOY_HUB_REPAIR_PUBKEY`, and
   each worker installs the shim and authorizes the key.

A fleet whose hub predates this revision reports no key, so
`install_hub_repair_access` returns immediately and the cohort deploys exactly
as it does today. Absence degrades to current behavior; it is not a failure.

Both steps run on the legacy one-shot node path only. A typed phase-2 deploy
consumes infrastructure receipts and is forbidden from mutating tunnel and
authorization state, so it does not touch `authorized_keys` — same rule the
reverse-tunnel key already follows.

## Operating it

```console
# On the hub: what is this worker doing?
ssh -i ~/.mac/keys/mac-hub-repair-id worker-1 status

# Restart a wedged agent.
ssh -i ~/.mac/keys/mac-hub-repair-id worker-1 restart agent

# Read the last 200 lines of a deploy log.
ssh -i ~/.mac/keys/mac-hub-repair-id worker-1 logs
ssh -i ~/.mac/keys/mac-hub-repair-id worker-1 tail deploy-20260820T000000Z.log
```

For a fleet that needs the bastion, let the module build the argv rather than
reconstructing the route by hand:

```console
python3 -m mac.hub_repair_key ssh-argv --agent worker-1 status
```

To narrow the entry further on a fleet with a stable hub address, add source
restrictions at install time — `--allow-from 100.64.0.0/10` becomes a
`from="…"` option on the authorized_keys entry.

## Scope

Deliberately outside this feature:

* **No configuration writes.** Every verb is diagnosis or a restart of something
  the deploy already manages. Re-pulling a deploy or rolling back a revision is
  a deploy operation with its own receipts and cohort semantics; exposing it
  behind a repair key would create a second, unjournaled path to the same state.
* **No hub-side automation.** Nothing calls these verbs automatically. The key
  is a path a human or an agent can use when the control plane cannot help;
  making the hub self-heal over it is a separate decision about what a hub may
  do unattended.
* **The tunnel key is untouched.** It keeps its current unrestricted entry
  because a reverse tunnel needs port forwarding, which is the first thing
  `restrict` takes away. Merging the two roles into one key would mean giving
  the repair role back the forwarding it should not have.
