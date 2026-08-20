---
name: setup-mac-fleet
description: Use when a user asks to set up, bootstrap, deploy, access, or configure mac agents or a fleet — including how to deploy onto bare-metal, virtual-machine, containerized, or provider-managed HGX agents. Runs the first-time setup wizard, writes a home-scoped multi-fleet registry, selects the right supervisor (systemd / launchd / supervisord) and SSH transport (direct / HGX / Tailscale / Headscale / bastion ProxyJump) per host type, and keeps fleet-specific data out of Git.
---

# Setup Mac Fleet

Use this skill when the user asks to set up or deploy a new mac fleet and
`~/.mac/fleets.yaml` or `~/.mac/.env` is missing, or asks how to deploy an
agent onto a specific host type (bare metal, VM, or container).

## Rules

- Do not invent agent names, hostnames, IP addresses, Slack channel names, or
  model selectors.
- Do not commit fleet topology or secrets. Fleet topology belongs in
  `~/.mac/fleets.yaml`; local deploy secrets belong in `~/.mac/.env`.
- Provider API keys (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, etc.) belong in
  `~/.mac/.env` — the wizard collects them and TokenHub absorbs them on first
  deploy. Do not put them in fleet YAML or any committed file.
- Keep committed fleet examples generic. Personal fleets must live only in the
  home-scoped fleet registry.
- The host type is not its own deploy command — it is a combination of `OS`,
  `supervisor`, and SSH transport. Pick those (or let `auto` detect), then use
  the same deploy commands below.

## Workflow

1. Run the wizard:

   ```bash
   bash setup.sh
   ```

2. If the user wants a non-default path, pass explicit paths:

   ```bash
   bash setup.sh --fleets-config ~/.mac/fleets.yaml --env-file ~/.mac/.env
   ```

3. The wizard opens with two required questions before anything else:
   - **"Are you running this on the machine being configured?"** — skips SSH
     target prompts and adjusts the Next-step instructions when yes.
   - **"Setting up a hub or a worker?"** — required, no default.
     - **hub**: creates a new fleet entry. The wizard asks for fleet topology,
       supervisor, Slack channel, per-agent Hermes models, worker mode, canary
       policy, shared Qdrant readiness, fleet network provider (Tailscale
       default; Headscale needs explicit login server, enrollment-key source,
       DNS assumption, and health URL), and **at least one upstream LLM
       provider** (nvidia / openai / anthropic / perplexity — API key required,
       base URL optional). The loop does not exit until at least one provider is
       entered.
     - **worker**: looks up the existing fleet by hub name, then asks only for
       the new worker's name, SSH target, OS, supervisor, mode, and canary
       policy.

4. `setup.sh` is the one-pass entrypoint. By default it writes the fleet
   registry/env file, sources the generated env file, and deploys the selected
   hub or worker immediately.

   To configure without deploying:

   ```bash
   bash setup.sh --configure-only
   ```

   Existing fleet deploy commands can still be run through `setup.sh`:

   ```bash
   bash setup.sh --hub <hub-node> [agent ...]
   bash setup.sh --new-hub <hub-node> --target user@host[:port]
   ```

   Provider keys in `~/.mac/.env` are forwarded through the SSH layer to
   `seed_or_merge_credentials()`, which writes them into
   `~/.tokenhub/credentials` on the hub.

5. If asked to inspect or edit the fleet later, edit
   `~/.mac/fleets.yaml`, not `deploy/fleet/config.yaml`.

## HGX direct worker access

Treat HGX as an additional direct SSH path for provider-managed workers, not as
an assumed dependency:

1. Confirm `$HOME/.local/bin/hgx` is present and executable.
2. Run `$HOME/.local/bin/hgx list` and use HGX only when it returns a worker
   relevant to the requested fleet operation.
3. Resolve the worker by immutable HGX session ID, especially when display
   names are duplicated or an instance has been recreated.
4. Connect with `$HOME/.local/bin/hgx ssh <session-id>`.

When working interactively and HGX is not authenticated, run
`$HOME/.local/bin/hgx login`, then retry `hgx list`. Do not launch that
interactive login flow from unattended automation; report the authentication
requirement instead.

HGX does not replace `~/.mac/fleets.yaml` as the registered MAC topology. After
using HGX to recover or replace a worker, reconcile its endpoint and attested
agent identity into the fleet registry so later deploy and SSH operations do
not use stale routing.

## Trust model: bootstrap transport and point-to-point SSH

Initial host bootstrap — for every domain (local, internal, or
HGX-provisioned external/remote) — **must** run over that domain's
configured transport: direct SSH for a local or internal host, `hgx ssh`
for an HGX-provisioned remote host. That transport is the only trusted,
secure channel that can run commands directly on the host and set it up;
there is no lower-trust fallback, and provider code must not invent one
(e.g. don't accept an unauthenticated HTTP endpoint, an in-cluster service
DNS name with no credential, or a "just curl it" shortcut as a substitute
for bootstrapping over SSH/`hgx ssh`).

What that bootstrap buys you is narrower than it looks: a working **hub**
with its own `MAC_API_TOKEN`, which can then be fetched (see step 3 of
QUICKDEMO.md) and handed to the other fleet members being brought up.
Beyond that, **SSH access from the provisioner (the operator session
running `setup.sh` / `make deploy`) to every agent is still assumed to work
point-to-point** — bringing up the hub does not give the hub, or the
provisioner, any new way to reach a worker. Each worker's bootstrap is a
separate direct-SSH (or `hgx ssh`) operation from the provisioner, exactly
like the hub's was.

**Known gap / future optimization (not implemented):** the hub could
generate its own SSH keypair during bootstrap and have that public key
appended to `~/.ssh/authorized_keys` on each worker as part of worker
bootstrap. That would let the hub reach and repair a worker on its own —
independent of the original provisioner's SSH access — instead of every
post-bootstrap fix needing to be re-run by whoever provisioned the fleet.
Track this as a fleet-management feature, not something to assume exists
today.

## Agent host types

The system runs the same mac agent (vendored Hermes runtime + control-plane /
worker) on three host types. `deploy/deploy-mac-fleet.sh` is the single deploy
path for all three; what changes per host is **OS**, **supervisor**, and **SSH
transport**. The wizard records these per node (`OS`, `supervisor`, `target`,
and the fleet's `ssh_jump` / network provider). Re-deploy any node with:

```bash
make deploy HUB=<node>           # or: bash setup.sh --hub <node>
```

**Mechanism shared by all host types.** The operator's local checkout HEAD is
the deployed revision (`git -C <repo> rev-parse HEAD` → `MAC_DEPLOY_GIT_REV`):
the script ships a release archive over SSH (and/or clones
`MAC_DEPLOY_GIT_URL@<branch>` then checks out that rev on the node), installs
into `~/.mac/venv` + `~/.mac/src`, writes env/topology, and (re)starts services
under the node's supervisor. Existing source + venv are backed up under
`~/.mac/backups/` first, so a bad deploy is recoverable. → Run the deploy from
the checkout whose origin is the fork that host runs and whose HEAD is the rev
you intend to ship; deploying a stale HEAD rolls the node *backward*.

**Supervisor** (`--supervisor`, wizard "supervisor", or `auto`). Auto-detect
in `deploy-mac-fleet.sh`:
- `OS=darwin` → **launchd**
- Linux with `systemctl` and `/run/systemd/system` → **systemd**
- otherwise (e.g. inside a container) → **supervisord**
  (`/etc/supervisor/conf.d` or `/etc/supervisord.d`)

**Hub reachability.** Mesh-joined hosts (Tailscale/Headscale) use the hub's
mesh URL directly. A spoke that cannot reach the hub directly (e.g. an
in-cluster pod) instead registers through a hub-managed **reverse tunnel**
(`http://127.0.0.1:<port>`); the deploy log says either "using tailscale hub
URL …; skipping reverse tunnel" or "restarted mac-agent with tunnel now
available".

### Bare-metal agents

- A physical host with a full init system.
- **OS:** `linux` (or `darwin` for a Mac). **Supervisor:** `systemd` on Linux,
  `launchd` on macOS — or `auto`.
- **Transport:** direct SSH (`target user@host[:port]`), normally on a
  Tailscale/Headscale mesh so the agent reaches the hub at its mesh IP.
- **Deploy:** `make deploy HUB=<node>`; first hub on a fresh box:
  `bash setup.sh --new-hub <node> --target user@host`.
- *Example:* the `mac` fleet hub `hub` (`<user>@<host>`) — Linux, systemd,
  Tailscale hub URL `http://<tailscale-ip>:8789`.

### Virtual-machine agents

- A cloud or local VM. To the deploy tooling this is **identical to bare
  metal** — a host with an OS and an init system reached over SSH. There is no
  VM-specific branch; treat a VM as a bare-metal host of the same OS.
- **OS / supervisor:** `linux`+`systemd` or `darwin`+`launchd` (or `auto`).
- **Transport:** direct SSH or Tailscale/Headscale mesh.
- **Deploy:** same commands as bare metal.
- *Example:* `mac` fleet workers `worker-1` / `worker-2` — Linux VMs/hosts under
  systemd, joined to the fleet mesh and reached by SSH.

### Containerized agents

An agent inside a container/pod with **no init system**. Two supported models:

1. **SSH-into-pod + supervisord** (operate the pod like a host). The pod runs
   `sshd`; the deploy reaches it over SSH and supervises the agent with
   **supervisord**. In-cluster pods (in-cluster DNS such as
   `*.svc.cluster.local`) are reached via a **bastion ProxyJump** declared
   fleet-wide in `~/.mac/fleets.yaml`:

   ```yaml
   ssh_jump: "user@bastion.example:2222"
   ```

   `deploy-mac-fleet.sh` applies `-o ProxyJump=<jump>` automatically (no
   `~/.ssh/config` edits), and the deploy prints
   `==> ssh: operator->node via -o ProxyJump=…`. Spokes register to the hub
   through the reverse tunnel rather than a mesh IP.
   - **OS:** `linux`. **Supervisor:** `supervisord` (`auto` selects it when
     systemd is absent).
   - **Deploy:** the same `make deploy HUB=<node>`; the fleet's `ssh_jump`
     routes it through the bastion.
   - *Worked example:* the generic GKE sample `deploy/fleet/samples/gke.fleet.yaml`
     — hub pod `gke-hub` + workers `gke-worker-1/2` under supervisord, reached
     via the bastion ProxyJump, workers registering through the reverse tunnel.
     Copy it with `scripts/setup-fleet.py --init-from gke --name <fleet>`, fill
     in the `<placeholders>`, then `--spec ~/.mac/specs/<fleet>.fleet.yaml`. The
     same `mac.fleet_setup.v1` schema covers EKS/AKS/OKE; see
     `deploy/fleet/samples/README.md`. (Real, named fleets live outside git in
     `~/.mac/specs/` — never check one in.)

2. **K8s-native, image-based** (`deploy/k8s/`). A stateless `mac-api`
   Deployment plus a `mac-runner` orchestrator that creates one Job per task,
   backed by an externally-managed Postgres (`MAC_DATABASE_URL`). Here the unit
   of deploy is a **container image**, not an SSH push:

   ```bash
   scripts/build-and-push-image.sh --registry <registry>   # build + push
   kubectl apply -k deploy/k8s/mac-api                      # and mac-runner
   ```

   or sync via ArgoCD from a platform-config repo. Use this for
   horizontally-scaled, no-SSH clusters. See `deploy/k8s/README.md`.

## Validation

Before deploy, run:

```bash
bash -n deploy/deploy-mac-fleet.sh
bash -n deploy/install-qdrant-service.sh
bash -n deploy/install-tailscale.sh
bash -n deploy/install-headscale.sh
.venv/bin/python -m pytest tests/test_deploy_agent_configs.py tests/test_hermes_startup.py
```

When touching the K8s-native (image-based container) path, also validate the
manifests render:

```bash
kubectl kustomize deploy/k8s/mac-api >/dev/null
kubectl kustomize deploy/k8s/mac-runner >/dev/null
```
