# Coding-CLI Credentials and Model Selection

The fleet's agents are the user, parallelized: autonomous instances acting
fully on the operator's behalf on more machines. That principle drives how
coding-CLI (claude / codex / cursor) authentication and model choice work.

## The workflow

1. **Start on your own machine.** Log in to whichever coding CLIs you want
   the fleet to use, exactly as you would for yourself (`claude` login,
   `codex login`, `cursor-agent login`). No fleet yet, no extra steps.
2. **Create the fleet.** Deploy hub + workers as usual. Workers report both
   credential configuration and a secret-free, end-to-end OpenShell route
   verification. The hub dispatches repository work only to a fresh matching
   provider/protocol/auth/model proof.
3. **Sync credentials on demand.**

   ```console
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
| codex | `MAC_CODEX_TOKEN` / `CODEX_API_KEY` / `OPENAI_API_KEY` → `~/.codex/auth.json` | environment auth is preferred; rotating file auth is not copied into OpenShell by default |
| claude | cloud identity → `ANTHROPIC_AUTH_TOKEN` → `ANTHROPIC_API_KEY` → `apiKeyHelper` → `CLAUDE_CODE_OAUTH_TOKEN` → local credentials | the matching environment/helper/cloud configuration |
| cursor | `CURSOR_AUTH_TOKEN` env → `CURSOR_API_KEY` env → macOS Keychain (`"cursor-access-token"`) | Login tokens as `CURSOR_AUTH_TOKEN`; generated API keys as `CURSOR_API_KEY` in `~/.mac/mac.env` |

macOS note: Claude Code and Cursor keep their tokens in the Keychain, not in
files. The sync exports them with `security find-generic-password` (only the
logged-in user can) and materializes the portable form on the worker. Cursor's
`cursor-access-token` entry is a browser-login token and must be projected as
`CURSOR_AUTH_TOKEN`; treating it as `CURSOR_API_KEY` makes Cursor attempt the
generated-API-key login flow and reject it.

**Transport rules** (same discipline as `mac fleet sync-token`): secrets move
only over the fleet's SSH routes, only on **stdin** — never argv, env,
stdout, or the hub ledger. The hub never sees the credential; it sees only
route fields, a SHA-256 route fingerprint, the verification time/result, and a
classified failure reason.

`configured` is deliberately weaker than `verified`. A present token can still
target the wrong wire protocol or a dead endpoint. `mac fleet creds-status`
therefore reports `ROUTE UNAVAILABLE (...)` separately from `NEEDS SYNC`.

Configured CLIs are verified in priority order until one succeeds. A failed
higher-priority route remains visible in the per-CLI report but does not shadow
a working fallback. An explicit `MAC_CODING_AGENT=<cli>` pin is different: it
restricts verification to that CLI and fails closed if the pinned route does
not work.

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

  ```console
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

  ```console
  mac observability list --name llm.route --limit 50
  ```

## Task deliverable kind (code vs report)

By default a task is a **code** deliverable: the fleet expects a repository
change and enforces the strict evidence contract (a pushed commit with changed
files and a passing contract test). That gate is load-bearing — it is what
stops an agent from claiming "done" with nothing — so it is not bypassable.

Some tasks legitimately produce **no code change**: investigate why X is
failing, triage an incident, answer a question, summarize the state of Y.
Declare those at creation and they are satisfied by a substantive
`operator_result` (a real summary / structured findings / artifacts) — no
diff, no branch:

```console
mac task create "why is the review loop stalling?" --kind report
mac task create "triage the failing GKE deploy"    --kind report
```

`--kind report` (aliases: `answer`, `analysis`, `investigation`, `question`,
`triage`) writes `metadata.deliverable = "report"`. API callers set the same
field on task creation. A report task gets no managed worktree/branch and the
repository finalizer never runs for it; the agent still has the `mac` CLI,
codegraph, and git to read whatever it needs. The declaration is an
operator/workflow-author decision at creation — an executing agent cannot set
it for itself, so it is not a way to dodge the substance gate (it is the
opposite of the `task_d7c51a0b` incident, where an executor *implicitly*
emitted `operator_result` for what was really a code task).

A report that must inspect a registered repository can opt in explicitly with
this versioned metadata in addition to `deliverable: report`:

```json
{
  "deliverable": "report",
  "report_repository_access": {
    "schema": "mac.report_repository_access.v1",
    "mode": "read_only"
  }
}
```

MAC then supplies the current repository contract and a detached, task-owned
clone of the current canonical base at `MAC_TASK_REPO_WORKTREE`. The clone has
no remote and repository credentials and ambient Git credential configuration
are withheld. Evidence remains `operator_result`; agent-authored test claims
are not trusted. MAC runs the current registered contract's `test.command` in a
separate credential-free OpenShell verifier and requires it to pass. Commits,
pushes, PRs, and the deterministic repository finalizer are not part of this
path. Any file, index, HEAD, or remote mutation fails the task. Missing,
malformed, or future unknown access declarations keep the default no-repository
report behavior.

This is also the right tool for **smoke-testing the fleet itself** ("report
which coding CLI you used") — but note that verifying MAC's own behavior
(routing, metering, sandboxing) is usually better done from the observability
ledger and on-host probes than by creating a task at all.

## Billing note

With CLIs authenticated, coding work bills to the CLI plans/accounts the
user logged in with, and MAC's ledger sees wall-time plus the CLI's own
telemetry only. Work routed through the fleet router (the fallback, and
everything Hermes-native) is token-metered per agent/task/model in
`llm.route`. Both paths are the user acting on the user's behalf — pick per
economics; `mac fleet creds-status` tells you which mode each agent is
actually in.
