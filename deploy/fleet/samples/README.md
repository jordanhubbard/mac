# Fleet samples (per-CSP, never per-fleet)

These are **generic, per-CSP** fleet-setup samples. They capture the *shape* of
deploying a mac fleet onto a given kind of infrastructure — not anyone's actual
fleet.

## The principle

The repo ships **generic, per-CSP** samples. A **per-user / per-fleet** spec
(real agent names, real bastion hosts, real cluster DNS, real model) lives
**outside Git** in `~/.mac/specs/<fleet>.fleet.yaml`, created at install time by
copying one of these samples and filling in the `<placeholders>`. The generated
registry (`~/.mac/fleets.yaml`) and secrets (`~/.mac/.env`, mode 0600) are also
outside Git.

**Never check a named/personal fleet into this repo.** It is a per-user/
per-fleet "bleed-through" (one operator's concrete topology baked into a repo
that must be generic for any fleet owner). If you find one, delete it and, if
its shape is reusable, fold that shape into a de-personalized `<csp>.fleet.yaml`
sample here. Every sample is marked `sample: true`; the deploy refuses to deploy
a `sample: true` config, and `doctor_checks` asserts `fleet.not_sample` for a
real deploy.

## CSP framing

Samples are organized by cloud / infrastructure kind, because the knobs that
differ are CSP-shaped, not owner-shaped:

| CSP / kind            | Typical bastion / reach        | Network provider          | DNS                          | Supervisor   |
| --------------------- | ------------------------------ | ------------------------- | ---------------------------- | ------------ |
| AWS / EKS             | bastion ProxyJump              | none (in-cluster)         | in-cluster `*.svc.cluster.local` | supervisord  |
| Azure / AKS           | bastion ProxyJump              | none (in-cluster)         | in-cluster `*.svc.cluster.local` | supervisord  |
| GCP / GKE             | bastion ProxyJump              | none (in-cluster)         | in-cluster `*.svc.cluster.local` | supervisord  |
| OCI / OKE             | bastion ProxyJump              | none (in-cluster)         | in-cluster `*.svc.cluster.local` | supervisord  |
| GCP / GCE (VM)        | direct SSH or mesh             | tailscale \| headscale    | public / mesh                | systemd      |
| Bare-metal / VM       | direct SSH or mesh             | tailscale \| headscale \| none | public / mesh / hostnames | systemd \| launchd |

The knobs that typically differ per-CSP:

- **bastion / ProxyJump** — operator→pod via a jump host (`ssh_jump`,
  `ssh_strict_host_key_checking`) vs direct SSH.
- **network provider** — `tailscale` | `headscale` | `none` (in-cluster DNS
  only; the hub is reached over the stock hub→spoke reverse tunnels).
- **DNS** — in-cluster (`<pod>.<namespace>.svc.cluster.local`) vs public /
  mesh hostnames.
- **supervisor** — `systemd` (Linux hosts) | `supervisord` (pods with no init
  system) | `launchd` (macOS).

GKE is provided as the **worked example** (`gke.fleet.yaml`). The same
`mac.fleet_setup.v1` schema covers every other CSP — the EKS / AKS / OKE shapes
are nearly identical (swap the in-cluster DNS and bastion), and the VM /
bare-metal shapes differ only in network provider, DNS, and supervisor.

**Contributors add a new `<csp>.fleet.yaml` sample here — not a personal
fleet.** Start from `gke.fleet.yaml`, keep it placeholder-only, and mark it
`sample: true`.

## Install flow

```bash
# List the available CSP samples (name + description).
scripts/setup-fleet.py --list-samples

# Copy a sample to ~/.mac/specs/<fleet>.fleet.yaml (mkdir -p; refuses to
# clobber without --force). --name defaults to the sample name.
scripts/setup-fleet.py --init-from gke --name my-gke

# Fill in the <placeholders> (agent names, bastion, cluster DNS, model, key env).
$EDITOR ~/.mac/specs/my-gke.fleet.yaml

# Materialize ~/.mac/fleets.yaml + ~/.mac/.env from the customized spec.
scripts/setup-fleet.py --spec ~/.mac/specs/my-gke.fleet.yaml --force
```
