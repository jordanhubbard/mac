#!/usr/bin/env bash
set -euo pipefail

# Fleet executors inherit deployment/task environment. Keep repository tests
# hermetic so they exercise the checked-out code, not the live agent runtime.
unset "${!ACC_@}"
unset "${!FIRECRAWL_@}"
unset "${!HERMES_@}"
unset "${!MAC_@}"
unset "${!QDRANT_@}"
unset "${!SLACK_@}"
unset "${!TOKENHUB_@}"

PATH=".venv/bin:${PATH}" exec .venv/bin/python -m pytest "$@"
