---
name: setup-mac-fleet
description: Use when a user asks to set up, bootstrap, deploy, or configure a new mac agent fleet. Runs the first-time setup wizard, writes a home-scoped multi-fleet registry, and keeps fleet-specific data out of Git.
---

# Setup Mac Fleet

Use this skill when the user asks to set up or deploy a new mac fleet and
`~/.mac/fleets.yaml` or `~/.mac/.env` is missing.

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

## Validation

Before deploy, run:

```bash
bash -n deploy/deploy-mac-fleet.sh
bash -n deploy/install-qdrant-service.sh
bash -n deploy/install-tailscale.sh
bash -n deploy/install-headscale.sh
uv run pytest tests/test_deploy_agent_configs.py tests/test_hermes_startup.py
```
