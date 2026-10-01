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

MAC no longer schedules dreams or naps: the hub's nap ticker and dreaming were
removed on 2026-09-30, and this installer starts no scheduler and no second
database authority. A standalone `com.mac.dream-cycle` launchd job left over from
an older install is not inspected or removed here; retire it by hand if present. Existing Hermes personality, memory, and gateway
services are outside this installer's scope.
