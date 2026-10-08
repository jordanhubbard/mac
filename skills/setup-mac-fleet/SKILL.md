---
name: setup-mac-fleet
description: Use when a user asks to set up, provision, update, or deploy mac hosts or a fleet (a hub plus Linux workers). Points at the manual "Provision a new host" checklist and the scripts/fleet-update updater in docs/operations/fleet-update.md, and keeps fleet-specific data out of Git.
---

# Setup Mac Fleet

Use this skill when the user asks to add a host to a mac fleet, rebuild one, or
move the fleet to a new commit.

## Rules

- Do not invent agent names, hostnames, IP addresses, Slack channel names, or
  model selectors. Ask.
- Do not commit fleet topology or secrets. Client topology belongs in
  `~/.mac/fleets.yaml`; the hub's worker list in `~/.mac/fleet-hosts`; host
  settings and tokens in `~/.mac/mac.env` (mode `0600`); client tokens in
  `~/.mac/.env`.
- Provider API keys (`NVIDIA_API_KEY`, `OPENAI_API_KEY`, etc.) never go in fleet
  YAML or any committed file.
- Keep committed examples generic. Personal fleets live only in the home-scoped
  files above.

## Workflow

All steps are in `docs/operations/fleet-update.md`. Read it before acting.

1. **New host:** follow its "Provision a new host" checklist, step by step. It
   covers the checkout at `~/.mac/src/mac`, the venv, `mac.env`, agent
   registration, the worker token (`mac admin worker-token issue --install`),
   the systemd units, OpenShell and Hermes.
2. **Update hosts:** on the hub, run
   `scripts/fleet-update --dry-run <hub|HOST|all> <sha>`, show the user the
   plan, then run it without `--dry-run`. It updates one host at a time and
   stops at the first failure.
3. **Failure:** use the rollback section for the host type (hub or Linux
   worker) in the same document. Never downgrade the database schema.

There is no setup wizard and no `make deploy`; both were deleted.
