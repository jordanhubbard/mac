# OpenClaw public identities and fleet representation

MAC separates internal agent identity from human-facing channel identity. A
fleet can have hundreds of workers while exposing one stable name such as
`MAC Hive` in Slack or Telegram. Individual agents receive a direct public
identity only when that is operationally useful.

## Objects and guarantees

- A **communication identity** is the stable name humans see. One enabled
  identity may be the fleet default.
- A **communication account** belongs to an identity and names one OpenClaw
  provider account, such as `slack/operations`. It stores only secret
  references and non-secret config.
- A **representation binding** says which identity speaks for an agent, role,
  project, or fleet. Resolution order is agent, role, project, fleet, then the
  default identity.
- A **gateway lease** gives one MAC agent fenced ownership of an account for
  outbound delivery. A stale fencing token cannot acknowledge or release work.
- A **human-message delivery** is a durable, idempotent outbox record. The hub
  retains its origin agent, task, target, attempts, lease, receipt, and error.

Representation modes are deliberately explicit:

- `direct`: the agent itself has the named public identity.
- `delegated`: a stable shared identity speaks for the agent.
- `internal_only`: the agent has no human-facing route.

OpenClaw is the only provider delivery path for an OpenClaw deployment. The
worker claims an outbox record only while it owns the matching gateway lease,
then invokes the pinned OpenClaw binary inside the existing OpenShell sandbox.
It never falls back to a host Slack or Telegram SDK.

## Minimal shared-identity setup

```console
mac admin communication identity configure mac-hive \
  --display-name "MAC Hive" --default

# The command returns the identity id. Use that id below.
mac admin communication account configure <identity-id> slack \
  --account-id operations \
  --credential-refs \
  '{"bot":"channel-identity.mac-hive.slack.operations.bot","app":"channel-identity.mac-hive.slack.operations.app"}' \
  --config '{"default":true}'

mac admin communication representation configure fleet mac \
  --identity <identity-id> --mode delegated
```

Provider credentials remain in the MAC vault. Fleet deployment recognizes the
identity-scoped names below; legacy per-agent names are temporary migration
fallbacks.

```text
channel-identity.mac-hive.slack.operations.bot
channel-identity.mac-hive.slack.operations.app
channel-identity.mac-hive.telegram.operations.bot
channel-identity.mac-hive.telegram.operations.canary_target
```

Set one deployed host's `hermes.public_identity` to `mac-hive`, its
`hermes.representation_mode` to `delegated`, and the corresponding account ids
to `operations`. Other hosts leave `public_identity` empty and set
`represented_by: mac-hive`; they run headless OpenClaw runtimes without human
channel credentials.

Exactly one active host may carry a given `public_identity` assignment. The hub
lease prevents duplicate **outbound** consumption, but Slack socket connections
and Telegram long polling begin when the OpenClaw gateway starts. For failover,
move the assignment to the standby and redeploy; do not run two listeners with
the same provider credentials.

## Sending and observing

Any represented agent can enqueue a message without owning a provider token:

```console
mac admin communication send channel:C012345 "Task completed" \
  --origin-agent-id agent_worker_1 \
  --channel slack \
  --idempotency-key task_123-completed
```

Useful inspection commands are:

```console
mac admin communication identity list
mac admin communication account list
mac admin communication representation resolve agent_worker_1
mac admin communication lease list --active-only
mac admin communication deliveries --limit 50
```

The Fleet IDE **Connections** view shows the same identity, account,
representation, and active-lease records. The Agents inspector distinguishes
`direct identity`, `delegate for`, and `represented by` so an OpenClaw runtime
is not mistaken for a unique public bot.

The REST resources use the same hierarchy under `/communication/identities`,
`/communication/accounts`, `/communication/representations`,
`/communication/gateway-leases`, and `/communication/deliveries`.
