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
  `~/.mac/.env`. The wizard collects them and wires the **in-mac router**; the
  deploy escrows each one into the hub's vault. Do not put them in fleet YAML
  or any committed file.
- Keep committed fleet examples generic. Personal fleets live only in the
  home-scoped registry (`~/.mac/fleets.yaml`) and `~/.mac/specs/`. Every
  shipped sample is marked `sample: true`, and the deploy refuses to deploy a
  sample.
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
   - **"Are you running this script on the machine being configured?"** —
     skips SSH target prompts and adjusts the next-step instructions when yes.
   - **"Setting up a hub or a worker?"** — required, no default.
     - **hub**: creates a new fleet entry. The wizard asks for fleet topology,
       supervisor, Slack channel, per-agent Hermes models, worker mode, canary
       policy, shared Qdrant readiness, fleet network provider (Tailscale
       default; Headscale needs explicit login server, enrollment-key source,
       DNS assumption, and IP prefix), and **at least one upstream LLM
       provider** (nvidia / openai / anthropic / perplexity — API key required,
       base URL optional). The loop does not exit until at least one provider is
       entered.
     - **worker**: looks up the existing fleet by hub name, then asks only for
       the new worker's name, SSH target, OS, supervisor, mode, and canary
       policy.

4. `setup.sh` is the one-pass entrypoint (it execs `setup.py`). By default it
   writes the fleet registry/env file, sources the generated env file, and
   deploys the selected hub or worker immediately.

   To configure without deploying:

   ```bash
   bash setup.sh --configure-only
   ```

   `--no-deploy` means the same thing. `--deploy` is a no-op, because
   deploy-after-config is already the default.

   Existing fleet deploy commands can still be run through `setup.sh`. Passing
   `--hub` or `--new-hub` short-circuits straight to
   `deploy/deploy-mac-fleet.sh` and skips the wizard entirely:

   ```bash
   bash setup.sh --hub <hub-node> [agent ...]
   bash setup.sh --new-hub <hub-node> --target user@host[:port]
   ```

5. **Provider keys land in the in-mac router. TokenHub is retired** — do not
   look for `~/.tokenhub/credentials`, and do not tell an operator their keys
   will be absorbed by TokenHub on first deploy. The wizard writes into
   `~/.mac/.env`:

   ```
   MAC_ROUTER_BACKEND=inproc
   MAC_ROUTER_PROVIDERS=<spec built from the keys just entered>
   ```

   Only the **hub** runs the router. It references each upstream key as
   `secret:<name>`, which the deploy escrows into the hub's encrypted vault.
   **Spokes carry no upstream key** — they route chat through the hub's `/v1`.
   If the gateway model selector is `*`, also set `MAC_ROUTER_DEFAULT_MODEL` in
   the env file.

6. If asked to inspect or edit the fleet later, edit `~/.mac/fleets.yaml`, not
   `deploy/fleet/config.yaml`.

## Non-interactive setup from a spec

A declarative `mac.fleet_setup.v1` spec is the reviewable path, and the one to
prefer for anything that will be repeated:

```bash
scripts/setup-fleet.py --list-samples
scripts/setup-fleet.py --init-from gke --name my-gke
scripts/setup-fleet.py --spec ~/.mac/specs/my-gke.fleet.yaml --force
```

Check a spec before it touches a host. Both of these read the spec and print a
report instead of deploying:

```bash
mac admin fleet validate --spec ~/.mac/specs/my-gke.fleet.yaml
mac admin fleet doctor --spec ~/.mac/specs/my-gke.fleet.yaml
```

`scripts/setup-fleet.py --validate-only --spec <path>` prints the same redacted
plan without going through the hub CLI.

## Which projects a worker may claim

A worker entry carries `allowed_projects`, and it is a **dispatch gate**, not a
label: a task whose project falls outside a worker's `allowed_projects` is
never offered to it, and the worker sits idle beside the queue. It reaches the
node as `MAC_WORKER_ALLOWED_PROJECTS`.

When an operator reports capable-but-idle agents, the diagnosis is one command:

```bash
mac task why-unclaimed <task-id>
```

It names the closed gate — including `agent_project_not_allowed`, "its
allowed_projects excludes this task's project". Fix it in `~/.mac/fleets.yaml`
and re-deploy that worker.

## Coding agents are part of the fleet's bill of materials

`mac.coding_agent.AGENT_PRIORITY` is the router's ordered preference list:
**opencode, pi, claude, codex, cursor**. The OpenShell sandbox image must carry
a binary for every one of them (`cursor` is the only entry whose binary differs
from its router key — the CLI is `cursor-agent`).

Adding an agent to the router therefore changes the sandbox BOM, which is a
frozen input to the image identity: the image has to be rebuilt, or tasks route
to a binary the sandbox does not have. That is not hypothetical — `opencode`
reached three workers, a policy render and a fleet inventory before anyone
noticed the image had no such binary. Check it rather than assuming:

```bash
mac admin sandbox-image bom --compare deploy/openshell/mac-hermes.Containerfile
mac admin openshell status --agent <agent>
```

## HGX provider-managed workers

Treat HGX as an additional direct SSH path for provider-managed workers, not as
an assumed dependency. mac talks to it through `mac.hgx_provider`, and that
adapter's contract is the advice:

1. **Address sessions by immutable session ID only.** A display name is never
   used to select a session; a name resolving to zero or several sessions is
   refused rather than guessed. This is what stops an operation landing on the
   wrong box after an instance has been recreated.
2. **Never run `hgx info`.** It can echo a fallback bootstrap password on
   stdout. The adapter refuses that verb outright; do the same by hand.
3. **Reachability is proved by executing something.** Finding a session in
   `hgx list` is not proof — the adapter attests with a nonce-bearing
   `hgx ssh <id> -- <command>`.
4. OpenShell/Docker execution needs the `standard-dind` session flavor.

For capacity work use the fleet-level verbs rather than the raw CLI; they carry
the state file, the cooldown, and the onboarding record:

```bash
mac admin hgx capacity status
mac admin hgx capacity plan
mac admin hgx capacity execute
mac admin hgx capacity mark-onboarded
```

`--hgx-binary` selects a non-default `hgx` executable. When HGX is not
authenticated, an interactive operator can log in and retry; do not launch an
interactive login from unattended automation — report the authentication
requirement instead.

HGX does not replace `~/.mac/fleets.yaml` as the registered MAC topology. After
using HGX to recover or replace a worker, reconcile its endpoint and attested
agent identity into the fleet registry so later deploy and SSH operations do
not use stale routing.

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
under the node's supervisor. The existing source tree and venv are backed up
first, as `~/.mac/backups/mac-src.<agent>.<ts>` and
`~/.mac/backups/venv.<agent>.<ts>`, so a bad deploy is recoverable. → Run the
deploy from the checkout whose origin is the fork that host runs and whose HEAD
is the rev you intend to ship; deploying a stale HEAD rolls the node
*backward*.

**Supervisor** (`--supervisor`, wizard "supervisor", or `auto`). Resolved on
the node by `detect_supervisor()` in `deploy/fleet-node-install.sh`, in this
order:
- macOS with `launchctl` → **launchd**
- Linux with `systemctl` **and** `/run/systemd/system` → **systemd**
- otherwise, if `supervisorctl` is present (e.g. inside a container) →
  **supervisord**
- otherwise the deploy stops and asks for an explicit
  `MAC_DEPLOY_SUPERVISOR`

An explicitly requested supervisor is verified rather than assumed: asking for
`systemd` on a node with no active systemd, or `supervisord` with no
`supervisorctl`, aborts the deploy instead of silently falling back to
something else.

**Hub reachability.** Mesh-joined hosts (Tailscale/Headscale) use the hub's
mesh URL directly. A spoke that cannot reach the hub directly (e.g. an
in-cluster pod) registers through a hub-managed **reverse tunnel** on
`127.0.0.1`, installed on the hub as a per-worker service. When a worker never
appears after an otherwise successful deploy, that tunnel is the first thing to
check: the node log says whether it became reachable, and shared-services
validation fails behind it.

### Bare-metal agents

- A physical host with a full init system.
- **OS:** `linux` (or `darwin` for a Mac). **Supervisor:** `systemd` on Linux,
  `launchd` on macOS — or `auto`.
- **Transport:** direct SSH (`target user@host[:port]`), normally on a
  Tailscale/Headscale mesh so the agent reaches the hub at its mesh IP.
- **Deploy:** `make deploy HUB=<node>`; first hub on a fresh box:
  `bash setup.sh --new-hub <node> --target user@host`.

### Virtual-machine agents

- A cloud or local VM. To the deploy tooling this is **identical to bare
  metal** — a host with an OS and an init system reached over SSH. There is no
  VM-specific branch; treat a VM as a bare-metal host of the same OS.
- **OS / supervisor:** `linux`+`systemd` or `darwin`+`launchd` (or `auto`).
- **Transport:** direct SSH or Tailscale/Headscale mesh.
- **Deploy:** same commands as bare metal.

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

   `deploy-mac-fleet.sh` applies `-o ProxyJump=<jump>` automatically, with no
   `~/.ssh/config` edits. Spokes register to the hub through the reverse tunnel
   rather than a mesh IP.
   - **OS:** `linux`. **Supervisor:** `supervisord` (`auto` selects it when
     systemd is absent).
   - **Deploy:** the same `make deploy HUB=<node>`; the fleet's `ssh_jump`
     routes it through the bastion.
   - *Worked example:* the generic GKE sample `deploy/fleet/samples/gke.fleet.yaml`
     — hub pod `gke-hub` plus workers `gke-worker-1` / `gke-worker-2` under
     supervisord, reached via the bastion ProxyJump, workers registering
     through the reverse tunnel. Copy it with
     `scripts/setup-fleet.py --init-from gke --name <fleet>`, fill in the
     `<placeholders>`, then `--spec ~/.mac/specs/<fleet>.fleet.yaml`. The same
     `mac.fleet_setup.v1` schema covers EKS/AKS/OKE; see
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
bash -n deploy/fleet-node-install.sh
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
