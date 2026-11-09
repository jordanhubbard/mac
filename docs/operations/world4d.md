# Running the World4D simulation after a reboot

The external World4D prototype supplies the simulation SDK, daemon and renderer.
`src/mac/world4d_service.py` supplies its missing process lifecycle: a macOS
LaunchDaemon starts the worlds at boot, independently restarts an exited or
stalled world, and shuts down each daemon's entire process group on service stop.
PostgreSQL retains the existing world objects and tick counters.

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

Readiness requires a valid world response and observed tick progress (or an
explicitly paused world). The prototype can keep its HTTP thread alive after
its simulation thread dies; a listening socket alone is insufficient. If
progress stops for `stall_seconds`, the supervisor restarts that world. Paused
worlds are healthy. The prototype currently unpauses a world when its daemon
starts, so service restart resumes simulation.

The prototype's own API/viewer retains its existing network binding and command
interface. Its access policy is separate from the loopback observation service.

## Restore the existing Hermes snapshot job

Keep the job paused until both worlds advance, a killed child automatically
recovers, and `/snapshot` renders successfully. Preserve its origin, schedule
and monitor history. Configure `monitor_url` to the supervisor's `/monitor` and
clear `monitor_script`; HTTP monitoring avoids launching a subprocess during
parallel MCP startup. An unchanged fingerprint suppresses the agent turn.

Update the prompt to fetch `/snapshot` and return the text for cron's automatic
delivery. It must not run the renderer's Slack-posting CLI or invoke another
message-sending tool: cron owns delivery. Validate an isolated canary using the
same job body with delivery disabled, then resume the original job with its
normal delivery configuration. Do not reset the monitor baseline merely to
force a test message. Unavailable worlds remain explicit monitor failures,
not a successful offline baseline.

Verify after reboot with launchd status, `/health`, advancing ticks, and a
successful scheduled monitor run. The deterministic process controls are in
`tests/test_world4d_service.py`; they exercise actual child exits, stalled
simulation, stable monitoring, rendering and child reaping.
