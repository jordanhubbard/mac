# Running the World4D simulation after a reboot

The external World4D prototype supplies the simulation SDK, daemon and renderer.
`src/mac/world4d_service.py` supplies its missing process lifecycle: a macOS
LaunchDaemon starts the worlds at boot, independently restarts an exited or
stalled world, and shuts down each daemon's entire process group on service stop.
A parent-liveness pipe also terminates daemon groups if the supervisor is
forcibly killed, so its replacement can reclaim the ports. PostgreSQL retains
the existing world objects and tick counters.

This is separate from `mac.world` and its MCP interface. Do not substitute those
modules for the external prototype's database schema or restore a demo snapshot
onto an existing world.

## Prepare a private deployment

Use an absolute Python interpreter and a source directory containing
`world4d_daemon.py`, its sibling modules, and `world4d_slack_post.py`. Install
`psql` and put its directory in the service's explicit `PATH`. The prototype
uses only Python's standard library plus that executable.

Create a private environment JSON file, mode `0600`, mapping environment names
to strings. Set `PATH`, `W4D_HOST`, `W4D_PORT`, `W4D_DB`, `W4D_USER`, and
`W4D_PASS` for the world store. Set `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`,
and `PGPASSWORD` for the fleet-memory bridge. Resolve actual connection settings
from the operator's local configuration; never commit them. The supervisor adds
bounded PostgreSQL connection and statement timeouts.

Create a local deployment JSON, using absolute paths and locally chosen ports:

```json
{
  "root": "/absolute/private/service-directory",
  "source": "/absolute/prototype-source-directory",
  "python": "/absolute/python3",
  "environment_file": "/absolute/private/environment.json",
  "user": "operator",
  "home": "/absolute/operator-home",
  "monitor_port": 17482,
  "worlds": [
    {"name": "example-test", "port": 17480, "hz": 1},
    {"name": "example-fleet", "port": 17481, "hz": 1}
  ],
  "poll_seconds": 3,
  "stall_seconds": 90
}
```

Prepare it from the reviewed repository revision:

```console
python3 src/mac/world4d_service.py prepare /absolute/deployment.json
```

Preparation copies the prototype's Python modules and behavior scripts into a
private runtime identified by their SHA-256 hashes. Later edits to the original
source directory do not change a running deployment. The generated
`service.json`, `service.py`, manifest and `com.mac.world4d.plist` stay under
`root`; that directory must remain available after logout. Prepare a new
runtime deliberately when updating the prototype. Retain the old runtime and
configuration for rollback. No database migrations run during preparation.

Install and start the generated LaunchDaemon:

```console
sudo -n install -o root -g wheel -m 0644 /absolute/private/service-directory/com.mac.world4d.plist /Library/LaunchDaemons/com.mac.world4d.plist
sudo -n launchctl bootstrap system /Library/LaunchDaemons/com.mac.world4d.plist
```

The job runs as the configured operator, with `RunAtLoad` and `KeepAlive`.
Supervisor and per-world logs are in `root`. An existing listener on a world
port causes startup to fail rather than adopting or killing another process.
Use `launchctl print system/com.mac.world4d` to inspect the job. Stop it with
`sudo -n launchctl bootout system/com.mac.world4d`; start it again with bootstrap.
For rollback, stop the job, restore the retained service configuration and
runner, then bootstrap. Never roll back world data to roll back this service.

## Readiness and snapshots

The supervisor exposes three endpoints on **loopback only** at `monitor_port`:

| Endpoint | Contract |
| --- | --- |
| `/health` | JSON readiness, child PIDs, ticks, errors and restart counts; HTTP 503 until every world is ready. |
| `/monitor` | Stable agent/danger/hub/pause fingerprint, excluding ticking counters; HTTP 503 when any world is unhealthy. |
| `/snapshot` | Text from the existing renderer for all configured worlds; never sends a Slack message. HTTP 503 on unready worlds or renderer failure. |
| `/snapshot-on-change` | JSON fingerprint and snapshot, cached until a notable change. Stable across ticks, with no terminal tool needed by cron. |

Readiness requires a valid world response and observed tick progress (or an
explicitly paused world). The prototype can keep its HTTP thread alive after
its simulation thread dies; a listening socket alone is insufficient. If
progress stops for `stall_seconds`, the supervisor restarts that world. Paused
worlds are healthy. The prototype currently unpauses a world when its daemon
starts, so service restart resumes simulation.

The child adapter supplies a threaded HTTP server so browser event streams do
not block API reads or readiness. Event peers use bounded socket writes and
exit when removed from the subscriber list after disconnect. The prototype's
own API/viewer retains its existing network binding and command interface. Its access policy is separate from the loopback observation service.

## Restore the existing Hermes snapshot job

Keep the job paused until both worlds advance, a killed child automatically
recovers, and `/snapshot` renders successfully. Preserve its origin, schedule
and monitor history. Configure `monitor_url` to `/snapshot-on-change` and clear
`monitor_script`; HTTP monitoring avoids launching a subprocess during parallel
MCP startup. An unchanged fingerprint returns the same cached snapshot and
suppresses the agent turn. Cached snapshots remain observations of the last
notable change, rather than continuous tick updates. The cache resets when the
supervisor restarts, which can produce one fresh observation.

Update the prompt to return the `snapshot` JSON field from the monitor's current
output for cron's automatic delivery. Set `enabled_toolsets` to `["no_mcp"]`;
that sentinel explicitly resolves to no tools. It must not fetch again, run the
renderer CLI or invoke another message-sending tool: cron owns delivery. Validate an isolated canary using the
same job body with delivery disabled, then resume the original job with its
normal delivery configuration. Do not reset the monitor baseline merely to
force a test message. Unavailable worlds remain explicit monitor failures,
not a successful offline baseline.

Verify after reboot with launchd status, `/health`, advancing ticks, and a
successful scheduled monitor run. The deterministic process controls are in
`tests/test_world4d_service.py`; they exercise actual child exits, stalled
simulation, stable monitoring, rendering and child reaping.

## Give fleet agents access to these same worlds

`mac.world4d_agent_mcp` is a standalone stdio adapter for the **pinned external
runtime** configured by this service. It is distinct from `mac.world_mcp` and
other prototypes with different databases or coordinate semantics. Install a
reviewed copy of `src/mac/world4d_agent_mcp.py` beside the private service runner.
The adapter reads `service.json` and its private environment on the simulation
host, so database credentials do not need to be distributed to clients.

On that host, configure a Hermes `mcp_servers.world4d` entry with an absolute
Python command and these arguments:

```json
["/absolute/private/world4d_agent_mcp.py", "--config", "/absolute/private/service.json", "--agent", "agent_example", "--world", "fleet"]
```

Remote agents can use their existing authenticated SSH transport as the command:

```json
{
  "command": "ssh",
  "args": ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "operator@simulation-host", "/absolute/python3", "-u", "/absolute/private/world4d_agent_mcp.py", "--config", "/absolute/private/service.json", "--agent", "agent_example", "--world", "fleet"],
  "enabled": true
}
```

Resolve the current target from the fleet registry and verify that each client
already authenticates before changing its config. Back up existing entries,
preserve unrelated settings, and install `skills/world4d-coordination/SKILL.md`
into that agent's skill directory. Restart or reload the gateway through its
supported lifecycle, preserving running work. Rollback restores the saved MCP
entry and skill; it never restores or clears world data.

For interactive sessions, deployment-owned runtime context can point agents at
the installed skill without editing their persona. Keep this guidance in its
own delimited block, preserve the existing Fleet and mood blocks, and verify
it remains present after a gateway reload.

Hermes cron jobs without a configured `workdir` deliberately skip deployment
context. Attach `world4d-coordination` through each existing fleet work job's
native `skills` list, preserving any prior skills. Verify the assembled cron
prompt actually contains the skill. Preserve the job's task prompt, schedule,
delivery, enabled toolsets and run state. Tool-free snapshot/reporting jobs do
not receive this attachment. Wait for ordinary scheduled turns rather than
triggering extra deliveries to manufacture an observation.

The adapter exposes observation, explicit presence, movement, status and directed
signals. Its configured identity owns the avatar and sender field; no agent
argument is accepted from a tool caller. Initialization and tool discovery make
no world writes. Receiving signals defaults to a non-consuming read. A signal
does not wake a peer; participation requires a real agent turn.

Measure rollout in stages: MCP discovery on every client, an explicitly labeled
operator probe across two identities, then actual agent tool calls and resulting
world events during ordinary turns. Keep probe actors separate from fleet actors.
Advancing simulation ticks, gateway startup and old avatar `online` flags are not
evidence of agent participation. The snapshot monitor still observes its documented
count/pause fingerprint; a changed note or signal alone need not trigger a post.
