---
id: mac-5bt
status: closed
deps: []
links: []
created: 2026-05-22T04:43:14Z
type: feature
priority: 1
assignee: Jordan Hubbard
mac-task-id: pending:mac-5bt
---
# Add Tailscale install+join to fleet onboarding wizard and deploy script

Every mac fleet agent needs to join a shared Tailscale tailnet so agents on different networks (e.g., Azure hub + NVIDIA worker) can reach each other. The wizard (scripts/setup-fleet.py) and deploy script (deploy/deploy-mac-fleet.sh) have no Tailscale step. Hub was deployed without Tailscale, so workers on different networks cannot register.\n\nNeeds:\n- deploy/install-tailscale.sh: install package, start tailscaled under detected supervisor (systemd/supervisord/launchd), run tailscale up --auth-key, wait for IP\n- Wizard prompts: Tailscale auth key (stored in ~/.mac/.env as MAC_DEPLOY_TAILSCALE_AUTH_KEY), hostname prefix, auto-update hub_url to use Tailscale MagicDNS name\n- Fleet config YAML: defaults.tailscale.install (auto/yes/no), defaults.tailscale.hostname_prefix\n- Deploy script: wire tailscale_install + tailscale_hostname_prefix into spec pipeline, call install-tailscale.sh before qdrant (qdrant binds to Tailscale IP if available)\n- Auth key is a secret — lives in env only, not YAML
