# Hub-Host Saturation Remediation

Read-only remediation report for the P3 hub-host congestion incident. It
documents the current held state of the hub host, recommends relocating the
container/OpenShell agent workloads off it so it can be resumed without
re-saturating the control plane, proposes a mitigation for the intermittent
`GET /agents` connection resets under load, and explains the congestion-collapse
feedback loop that ties the two together. This document changes no source or
configuration; every recommendation is a proposal for the operator to apply.

Throughout, `<hub-host>` is the node that runs the control-plane process (the
FastAPI ledger API). In this incident that node is currently **dispatch-held**
with the reason `hub host resource isolation: Docker/OpenShell workload
saturation`. The hold protects the control plane by keeping new task dispatch
off the saturated node, but it also removes a full worker node from the pool
and coincides with degraded hub-hosted services.

## 1. State of the incident

- The hub host runs the single control-plane API process. Production serves it
  as one Uvicorn worker: `uvicorn mac.api:create_app --factory --host 127.0.0.1
  --port 8000 --workers 1` (`deploy/systemd/mac.service:16`); the node-install
  path uses the same single-worker invocation
  (`deploy/fleet-node-install.sh:10405`). The systemd unit caps the process at
  `MemoryMax=1G` / `MemoryHigh=512M`.
- The hub host is currently HELD via the per-agent dispatch-hold mechanism
  (`ControlPlane.set_agent_dispatch_hold`, surfaced as `mac agent hold <id>
  --reason ...`, `src/mac/cli.py:2905`). A held agent is skipped during
  claim-next, so the hub node accepts no new tasks while the hold stands. The
  held state and reason are visible per row in `mac agent list --health`
  (`src/mac/cli.py:2641`), which sets `dispatch_hold` and
  `unconsumed_control_stream_age_seconds` on each agent.
- Observed degradation: the `GET /agents` HTTP endpoint intermittently resets
  the connection (`ConnectionResetError`) under load, while task endpoints stay
  up. This is consistent with the endpoint's shape (section 3): `GET /agents`
  materializes the entire agents table on every call, on the single shared
  Uvicorn worker, so it is the first endpoint to suffer when the hub host's CPU,
  memory, or DB-read budget is exhausted.

Net effect of the hold: the control plane is protected (no new dispatch onto the
saturated node), but the fleet is down one worker node and the hub-hosted
read endpoints remain fragile because the underlying saturation source — the
co-located container/OpenShell agent workloads — is still resident on the hub
host. The hold treats the symptom (dispatch) and not the cause (co-tenancy).

### Evidence pointers

- `mac agent list --health` — per-agent `dispatch_hold`, `dispatch_hold_reason`
  (rendered at `src/mac/cli.py:396`), and control-stream backlog age. Confirms
  the hub host is held and how stale its control stream is.
- `deploy/systemd/mac.service:16` and `deploy/fleet-node-install.sh:10405` —
  the single-worker Uvicorn service definition and its memory caps.
- `src/mac/api.py:6405` (`GET /agents`) and `src/mac/services.py:15352`
  (`ControlPlane.list_agents`) — the unbounded full-table read behind the
  resets.

## 2. Relocate container/OpenShell workloads off the hub host

The durable fix is to make the hub host carry the control plane only, and to
move the container/OpenShell agent workloads to non-hub worker nodes. That lets
the hold be released without immediately re-saturating the same node.

Proposed sequence (operator to apply; nothing here is executed by this report):

1. Identify what is co-resident. On the hub host, enumerate the container and
   OpenShell agent workloads sharing its CPU/memory with the control plane.
   Cross-reference `mac agent list --health` to see which agent identities are
   pinned to the hub host and are still marked healthy/busy.
2. Drain, don't kill. For each workload to relocate, place a dispatch hold with
   a clear reason (`mac agent hold <agent-id> --reason "relocating off hub
   host"`), let in-flight leases finish, then move the workload to a worker
   node with headroom. Draining avoids the retry storm described in section 4.
3. Re-home the workloads on worker nodes. Container/OpenShell execution should
   run on worker nodes, not the control-plane node. The hub host keeps only the
   ledger API (and, where used, the standalone router process described in
   `docs/hub-availability.md`, which already exists to move inference load off
   the ledger event loop).
4. Verify capacity headroom BEFORE resuming the hub host (below).
5. Resume the hub host: `mac agent resume <hub-host-agent-id>`
   (`src/mac/cli.py:2910`). Because the workloads are gone, resuming restores
   the node to the pool without re-loading the control plane.

### Verifying headroom before resume

Do not resume until all of the following hold, so the resume cannot immediately
re-saturate:

- Host resource headroom on the hub host: sustained CPU below a safe ceiling and
  resident memory comfortably under the service cap (`MemoryHigh=512M` /
  `MemoryMax=1G` in `deploy/systemd/mac.service`). If the control plane alone is
  near the memory cap, resuming will not help; investigate the ledger process
  first.
- Endpoint stability under representative load: `GET /agents` and `GET /health`
  return consistently with no `ConnectionResetError` across a burst of
  concurrent calls (this is the endpoint that fails first, so it is the canary).
- Control-stream backlog draining: in `mac agent list --health`, the hub host's
  `unconsumed_control_stream_age_seconds` should be low and trending down, not
  growing — a growing backlog means the node is still behind and resuming will
  add load it cannot absorb.
- Destination headroom: the worker nodes that received the relocated workloads
  are healthy in `mac agent list --health` and not themselves saturated.

## 3. Mitigating the `GET /agents` reset under load

Root shape of the endpoint:

- `GET /agents` handler returns `[agent.to_dict() for agent in cp.list_agents()]`
  with no pagination, no limit, and no caching (`src/mac/api.py:6405`).
- `ControlPlane.list_agents` runs `SELECT * FROM agents WHERE deleted_at IS NULL
  ORDER BY name, id` and builds a full `Agent` object per row every call
  (`src/mac/services.py:15352`). Every request re-reads and re-serializes the
  entire (non-deleted) agents table.

Under saturation on a single Uvicorn worker with a 1 GiB memory ceiling, this
unbounded full-table read is the natural first casualty: it holds a DB read and
builds a large response while the one worker is already starved, so the accept
backlog overflows and clients see `ConnectionResetError`. Task endpoints, which
touch bounded per-task rows, stay up because they never materialize the whole
table.

Proposed mitigations, cheapest first (all are proposals; implement in a separate
change):

1. Bound and paginate the response. Add a limit/cursor to `GET /agents` (and a
   server-side default cap) so a single call can never materialize the entire
   table. This is the smallest change with the largest effect on the reset.
2. Add a short-TTL cache for the full-list read. Agent inventory changes far
   more slowly than it is polled; a small server-side cache (invalidated on
   agent create/update/hold) removes repeated full-table scans from the hot
   path without changing the response contract.
3. Isolate the endpoint from event-loop starvation. Confirm the read runs off
   the event loop (the handler is a sync `def`, so FastAPI already dispatches it
   to the threadpool) and, if the threadpool is the bottleneck, size it
   explicitly; keep large serialization off the single async worker.
4. Bound server concurrency deliberately. Because the service runs `--workers 1`
   with a hard memory cap, set an explicit Uvicorn concurrency limit
   (`--limit-concurrency`) and keep-alive timeout so excess load is rejected
   cleanly (`503`) rather than resetting mid-response. Right-size `--workers`
   only after the memory footprint per worker is known, since the systemd cap is
   1 GiB total.
5. Give heavy readers a cheaper path. Callers that poll `mac agent list
   --health` for a few fields do not need every column of every agent; a slim
   projection (id, health, dispatch_hold, control-stream age) would cut both DB
   and serialization cost for the common health poll.

Success criterion for this section: `GET /agents` returns consistently under a
representative concurrent burst with zero `ConnectionResetError`, both while the
hub host is loaded and after resume.

## 4. The congestion-collapse feedback loop

The incident is a classic congestion-collapse loop, not a single failure:

1. Saturation. Container/OpenShell workloads co-resident on the hub host consume
   CPU/memory that the single control-plane worker needs.
2. Degraded hub services. The starved worker makes hub-hosted reads fragile —
   `GET /agents` resets, review/verification/DB-backed calls slow down or time
   out.
3. More failures. Tasks that depend on those hub calls (review, contract
   verification, ledger writes) fail or time out — not because the work is
   wrong, but because the hub could not answer in time.
4. Retry storm. Failed tasks are retried; clients that got a reset reconnect and
   re-poll. Retries and reconnects are themselves hub calls, so they add load to
   the already-saturated node.
5. Back to step 1, worse. The added retry/reconnect load deepens saturation,
   which degrades hub services further, which fails more tasks — the loop tightens
   until throughput collapses.

Relieving the hub host breaks the loop at step 1, its only stable cut point.
Moving the container/OpenShell workloads off the node removes the saturation
source, so the control-plane worker regains CPU/memory headroom; hub reads stop
failing (step 2), dependent tasks stop failing for infrastructure reasons
(step 3), and the retry storm drains instead of compounding (step 4). Holding
dispatch (the current state) only stops *new* work from arriving; it does not
remove the resident workloads, so it slows the loop without cutting it. The
`GET /agents` mitigations in section 3 raise the load ceiling and make the
endpoint fail cleanly (reject rather than reset), which prevents reset-driven
reconnect amplification from re-igniting the loop after resume.

## 5. Success criteria

- Hub host resumable: after the container/OpenShell workloads are relocated and
  the headroom checks in section 2 pass, `mac agent resume <hub-host-agent-id>`
  returns the node to the pool without the control-plane worker re-saturating
  (memory stays under the systemd cap, control-stream backlog drains).
- `GET /agents` stable under load: the endpoint returns consistently across a
  representative concurrent burst with zero `ConnectionResetError`, both under
  load and post-resume, once the section-3 mitigations are applied.

## 6. Scope note

This is a findings-and-recommendation report only. No source, test, deploy, or
runtime configuration was changed. The dispatch-hold/resume verbs, the
`GET /agents` handler, and `ControlPlane.list_agents` are referenced by
`file:line` as evidence pointers, not modified. Implementation of the pagination
/ caching / concurrency-limit mitigations and the workload relocation is left to
the operator and a separate change.
