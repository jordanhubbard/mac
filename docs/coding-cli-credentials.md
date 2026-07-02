# Coding-CLI Credentials and Model Selection

The fleet's agents are the user, parallelized: autonomous instances acting
fully on the operator's behalf on more machines. That principle drives how
coding-CLI (claude / codex / cursor) authentication and model choice work.

## The workflow

1. **Start on your own machine.** Log in to whichever coding CLIs you want
   the fleet to use, exactly as you would for yourself (`claude` login,
   `codex login`, `cursor-agent login`). No fleet yet, no extra steps.
2. **Create the fleet.** Deploy hub + workers as usual. Workers report their
   per-CLI auth status (secret-free) in every heartbeat, so the hub always
   knows who can run which CLI.
3. **Sync credentials on demand.**

   ```bash
   mac fleet creds-status                # who has what; who NEEDS SYNC
   mac fleet creds-sync --fleet <name>   # push from THIS workstation
   ```

   `creds-sync` is **lazy by default**: with no `--agent`, it targets only
   agents whose own reports say a CLI is on PATH but unauthenticated —
   credentials are never pushed where they aren't needed. Every push is
   verified: the worker re-runs its detector and the command prints the
   per-agent verdict.

## Where credentials come from (verified per CLI)

| CLI | Sources, in priority order | Delivered to the worker as |
|---|---|---|
| codex | `~/.codex/auth.json` (+ `config.toml`) | the same files, mode 0600 |
| claude | `ANTHROPIC_API_KEY` env → `~/.claude/.credentials.json` → macOS Keychain (service `"Claude Code"`) | `ANTHROPIC_API_KEY` in `~/.mac/mac.env`, or the credentials file |
| cursor | `CURSOR_API_KEY` env → macOS Keychain (`"cursor-access-token"`) | `CURSOR_API_KEY` in `~/.mac/mac.env` |

macOS note: Claude Code and Cursor keep their tokens in the Keychain, not in
files. The sync exports them with `security find-generic-password` (only the
logged-in user can) and materializes the portable form on the worker.

**Transport rules** (same discipline as `mac fleet sync-token`): secrets move
only over the fleet's SSH routes, only on **stdin** — never argv, env,
stdout, or the hub ledger. The hub never sees the credential; it only sees
the workers' secret-free status reports.

## Roaming workstations

You can only be interactive in one place at a time, and that place holds
your freshest logins. When a worker's CLI auth expires or is lost:

- the worker's next heartbeat flags it (`creds-status` shows **NEEDS SYNC**,
  and the status block is visible to the IDE/dashboard through the same
  agent resources);
- from whichever workstation you're currently on, `mac fleet creds-sync`
  re-syncs from that environment.

Nothing is pushed proactively — stale laptops at home can't clobber a fresh
login from the machine you're actually using.

## Choosing models (fleet-wide, per-agent, per-task)

- **Fleet/agent default**: the deployed gateway model
  (`hermes.gateway_model` in fleets.yaml / `MAC_HERMES_GATEWAY_MODEL`).
- **Per task** — unobtrusive, one flag:

  ```bash
  mac task create "port the parser" --model azure/anthropic/claude-haiku   # cheap
  mac task create "redesign locking" --model azure/anthropic/claude-opus   # strong
  ```

  This writes `metadata.model`; the worker exports `MAC_TASK_MODEL`; the
  executor maps it to the runtime's `--model` (Hermes) or the CLI's model
  flag (`claude --model`, `codex exec --model`, `cursor-agent --model`).
  API callers set the same `metadata.model` field on task creation —
  no new endpoint.

- **Observability**: every completion is recorded in `llm.route` with the
  requested and resolved model, token usage (streamed responses included),
  and the agent/task/lease that spent it — so per-task and per-model cost is
  a query, not a guess:

  ```bash
  mac observability list --name llm.route --limit 50
  ```

## Billing note

With CLIs authenticated, coding work bills to the CLI plans/accounts the
user logged in with, and MAC's ledger sees wall-time plus the CLI's own
telemetry only. Work routed through the fleet router (the fallback, and
everything Hermes-native) is token-metered per agent/task/model in
`llm.route`. Both paths are the user acting on the user's behalf — pick per
economics; `mac fleet creds-status` tells you which mode each agent is
actually in.
