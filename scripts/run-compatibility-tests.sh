#!/usr/bin/env bash
set -euo pipefail

# Protect import, public API/CLI serialization, UI API compatibility and the
# real hub/worker process seam on the same reviewed Python as the fleet.
exec "$(dirname "$0")/run-contract-tests.sh" \
    tests/test_control_plane_public_contract.py \
    tests/api/test_task_read_endpoints.py \
    tests/cli/test_cli_coverage_gate.py \
    tests/ui/test_fleet_ide_api_contracts.py \
    tests/test_worker_process_e2e.py
