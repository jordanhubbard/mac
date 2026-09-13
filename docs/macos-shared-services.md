# macOS shared services

Run `bash deploy/install-macos-services.sh` as the intended service account.
It delegates to `deploy/install-qdrant-service.sh`, which owns the user launchd
job `com.<FLEET_NAME>.qdrant`. The shared installer creates `Library/LaunchAgents`,
renders runtime paths, checks Qdrant readiness, and compensates a failed launchd
replacement. A readiness failure exits nonzero; existing vector data is retained.

Configure the installation with the existing shared-service variables:
`MAC_HOME` (default `$HOME/.mac`), `LOG_DIR`, `FLEET_NAME` (default `mac`),
`QDRANT_DATA_DIR`, `QDRANT_BIND_ADDR`, and `QDRANT_PORT`. Use the existing data
directory when updating a working installation. The wrapper and service env live
under the selected `MAC_HOME`; the launchd plist belongs to the invoking account.

Dream scheduling remains inside the hub's `NapTicker`, controlled by
`MAC_NAP_TICK_ENABLED` and its existing configuration. The nap cycle calls
`run_dream_cycle` against the hub's database authority and performs memory
promotion. This installer neither enables that scheduler nor starts a second
database authority. Check the hub's nap status and configuration to establish
that scheduling is enabled and working; a successful Qdrant install does not
prove it.

The former `com.mac.dream-cycle` plist embedded an account-specific Python path
and database URL. It also scheduled dreams independently of the hub's existing
nap cycle. The installer no longer creates this job or copies static plists.
If an existing standalone dream plist or loaded job is found, installation stops
before changing services. Inspect that job's actual command, current activity,
and hub scheduling before retiring or migrating it. An operator-customized job
is preserved, not silently overwritten or removed. Existing Hermes personality,
memory, and gateway services are outside this installer's scope.
