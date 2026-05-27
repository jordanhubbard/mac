#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARGS=()
CONFIG_ONLY=0
DRY_RUN=0
DEPLOY_DIRECT=0

for arg in "$@"; do
  case "$arg" in
    --configure-only|--no-deploy)
      CONFIG_ONLY=1
      ;;
    --dry-run)
      DRY_RUN=1
      ARGS+=("$arg")
      ;;
    --deploy)
      ;;
    --hub|--hub=*|--new-hub|--new-hub=*)
      DEPLOY_DIRECT=1
      ARGS+=("$arg")
      ;;
    *)
      ARGS+=("$arg")
      ;;
  esac
done

if [ "$CONFIG_ONLY" != "1" ] && [ "$DEPLOY_DIRECT" = "1" ]; then
  exec bash "$ROOT/deploy/deploy-mac-fleet.sh" "${ARGS[@]}"
fi

if [ "$CONFIG_ONLY" = "1" ] || [ "$DRY_RUN" = "1" ]; then
  exec python3 "$ROOT/scripts/setup-fleet.py" "${ARGS[@]}"
fi

PLAN_FILE="$(mktemp "${TMPDIR:-/tmp}/mac-setup-plan.XXXXXX")"
trap 'rm -f "$PLAN_FILE"' EXIT

python3 "$ROOT/scripts/setup-fleet.py" --deploy-plan-file "$PLAN_FILE" "${ARGS[@]}"

if [ ! -s "$PLAN_FILE" ]; then
  exit 0
fi

DEPLOY_ENV_FILE="$(
  python3 - "$PLAN_FILE" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
print(plan.get("env_file") or "")
PY
)"

if [ -n "$DEPLOY_ENV_FILE" ] && [ -f "$DEPLOY_ENV_FILE" ]; then
  set -a
  # shellcheck source=/dev/null
  . "$DEPLOY_ENV_FILE"
  set +a
fi

DEPLOY_ARGS=()
while IFS= read -r -d '' item; do
  DEPLOY_ARGS+=("$item")
done < <(
  python3 - "$PLAN_FILE" <<'PY'
import json
import sys
from pathlib import Path

plan = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
hub = str(plan.get("hub") or "").strip()
if not hub:
    raise SystemExit("setup plan missing hub")
items = ["--hub", hub]
items.extend(str(agent).strip() for agent in plan.get("agents") or [] if str(agent).strip())
for item in items:
    sys.stdout.write(item + "\0")
PY
)

exec bash "$ROOT/deploy/deploy-mac-fleet.sh" "${DEPLOY_ARGS[@]}"
