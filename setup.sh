#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARGS=()
CONFIG_ONLY=0

for arg in "$@"; do
  case "$arg" in
    --configure-only|--no-deploy)
      CONFIG_ONLY=1
      ;;
    *)
      ARGS+=("$arg")
      ;;
  esac
done

if [ "$CONFIG_ONLY" != "1" ]; then
  for arg in "${ARGS[@]}"; do
    case "$arg" in
      --new-hub|--new-hub=*)
        exec bash "$ROOT/deploy/deploy-mac-fleet.sh" "${ARGS[@]}"
        ;;
    esac
  done
fi

exec python3 "$ROOT/scripts/setup-fleet.py" "${ARGS[@]}"
