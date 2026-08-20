# Refused Agent Registration Runbook

An agent that reads `offline` in `mac agent list` used to mean one of two very
different things: the host is switched off, or the host is running, asking to
join every few seconds, and being turned away. This runbook covers the second
case — a registration the hub **refuses** because `machine.resources` or
`agent.resources` exceeds the 64 KB limit.

Background and the design decision: [ADR 0025](adr/0025-a-refused-registration-is-a-fleet-state.md).

## 1. Is it refused, or is it off?

From the hub only. You should not need to reach the host to answer this.

```bash
mac agent list                          # every row carries registration_state
mac agent list --selector 'name=~.'     # same, narrowed
```

- `registration_state: "accepted"` + `status: "offline"` — the host stopped
  talking. Ordinary offline triage.
- `registration_state: "refused"`, `registered: true` — the agent has a row from
  an earlier successful registration and its **latest** attempt was turned away.
- `registration_state: "refused"`, `registered: false` — the host has never been
  admitted. There is no agent row; this entry is synthesised from the refusal
  record, and `status`/`health_status` read `refused`.

The refusal detail is on the row under `registration_refusal`, or directly:

```bash
curl -s "$MAC_URL/agents/registration-refusals" | jq .
```

Each entry is one host, folded across its restart loop:

| field | meaning |
| --- | --- |
| `refusal_count` | attempts in the window — a high count *is* the crash loop |
| `field` | `machine.resources` or `agent.resources` |
| `size_bytes` / `limit_bytes` | what it weighed against what is allowed |
| `top_contributors` | the top-level keys that account for it, largest first |
| `message` | the one-line diagnosis, ready to paste |

The window is 15 minutes by default (`within_seconds`). A host that has been
fixed stops appearing.

The observability console (`/ui/console`, Fleet view) shows the same thing: a
`refused` tile, a critical banner naming each host and its cause, and a
`▲ never admitted` marker on the row.

## 2. Confirm it is size, not something else

```bash
mac observability list --name registration.refused --limit 20
```

`detail.top_contributors[0]` names the block to look at. In the 2026-08-20
incident it was `commands` — the worker's executable inventory — at 59 KB of a
65 KB payload.

## 3. Fix it

Current workers do this themselves. A worker running this code:

- bounds its command inventory to 16 KB of JSON before sending;
- sheds the largest unprotected block when its payload reaches the `critical`
  band (90%) and logs what it dropped;
- if the hub refuses anyway, sheds hard and **retries once** rather than exiting,
  so it joins the fleet degraded instead of crash-looping.

So a host that is still being refused is running an older agent, or is being
refused for something that cannot be shed. Options, in order:

1. **Update the agent on that host.** This is the fix; everything below is a
   stopgap.
2. **Shrink the inventory at the source.** `MAC_WORKER_COMMAND_INVENTORY_MAX_BYTES`
   (default 16384) and `MAC_WORKER_COMMAND_INVENTORY_MAX` (default 10000) bound
   it. Lowering the byte cap is safe: explicitly probed toolchain names are kept
   first, and truncation costs the incidental tail.
3. **Trim the PATH the agent runs with.** A host with a very fat exec path grows
   the inventory faster than the fleet's design does.

Do **not** raise `MAX_REGISTRATION_PAYLOAD_BYTES` to clear an incident. It moves
the same cliff further out and makes the next crossing more expensive.

## 4. Catch the next one before it crosses

The gauge is on every agent row:

```bash
mac agent list | jq '.[] | {name, resources_bytes, resources_utilization, resources_band}'
```

Bands: `ok` (<75%), `warn` (75–90%), `critical` (90–100%), `over` (refused).

Band **transitions** are recorded as metrics, not polled state, so they are
cheap to alert on:

```bash
mac observability list --name registration.payload_pressure --limit 50
```

Each carries `band`, `previous_band`, `utilization` and `top_contributors`.
`warn` is deliberately far from the limit — these payloads grow by kilobytes per
deploy, not per second, so 75% is days to weeks of warning.

## 5. What this does not cover

- A refusal for a reason other than size (auth, tenant binding, a malformed
  body) is not recorded here. Those fail loudly at the client and do not produce
  the silent `offline` ambiguity this runbook exists for.
- The heartbeat measures but never refuses. An agent whose stored resources
  already exceed the limit keeps working and keeps reporting `critical`/`over`
  pressure; it will only be refused the next time it re-registers, which is why
  the pressure metric is the signal to act on.
