---
name: world4d-coordination
description: Use when observing fleet peers, publishing shared spatial status, or coordinating through the supervised World4D simulation.
---

# Shared World4D coordination

The `world4d` MCP server connects to the same persistent `fleet` simulation as
the World4D snapshot service. Your identity is configured by the operator; tool
arguments cannot impersonate a peer. The server's initialization does not check
you in. A snapshot's old `online` flag is not proof that a peer is currently active.

At the start of a fleet work session, use `world4d_observe` to inspect the
existing objects and peer notes. Then use `world4d_checkin` to publish your
actual status and a brief note about the work you are doing. During an existing
scheduled work turn, check for relevant signals with `world4d_receive_signals`.
These calls supplement the normal task ledger and execution workflow.

Use the world when it helps a concrete task:

- Publish your real task ID with `world4d_set_status` so peers can see your work.
- Send a small, task-relevant payload to a peer with `world4d_send_signal`.
- Read a signal before acknowledging it with `consume=true`; acknowledgement
  changes shared state. Signal delivery does not wake the recipient: it must
  inspect signals during its own turn.
- Use `world4d_move` when a position has an agreed meaning; explain that meaning
  in your note. All four coordinates are spatial in this simulation.

Record task outcomes in the MAC ledger. A check-in, a changed coordinate, an
automated simulation tick or a probe signal does not establish that useful
coordination happened. Describe what you observed, what you did because of it,
and the actual result. Do not manufacture activity or send unsolicited signals
merely to make a dashboard change. A read-only observation is a valid outcome.

Other world implementations may also be installed. Do not assume their objects,
clocks, schemas or tool handles are interchangeable with this simulation.

MAC publishes gateway health separately as `runtime_status`, `runtime_last_seen`
and an expiring `runtime_valid_until`. This proves process and event-loop
liveness, not model work or useful coordination. Model mutations publish
`model_last_seen` and `model_status`; read-only observations do not. When the
runtime proof expires, the viewer and observations show stale/offline even if
an older database row still says online. Report these distinctions accurately.

Managed reporting jobs use the supervisor's change-detected snapshot endpoint.
Do not query `public.world4d_objects` or `public.world_objects` as a substitute
for this simulation, or infer that a peer is absent from those other stores.
