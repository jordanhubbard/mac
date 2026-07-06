#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ -x ".venv/bin/python" ]; then
    PY=".venv/bin/python"
else
    PY="$(command -v python3 || command -v python || true)"
fi
if [ -z "${PY:-}" ]; then
    echo "run-sanity-tests.sh: no Python interpreter" >&2
    exit 2
fi

selection="$(mktemp)"
trap 'rm -f "$selection"' EXIT
"$PY" scripts/select-sanity-tests.py "$@" >"$selection"
"$PY" - "$selection" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
print("sanity selection: %s (%s)" % (doc["mode"], doc["reason"]))
for path in doc.get("changed_files", []):
    print("  changed: " + path)
for path in doc.get("tests", []):
    print("  test: " + path)
PY

mode="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1]))["mode"])' "$selection")"
if [ "$mode" = "full" ]; then
    rm -f "$selection"
    trap - EXIT
    exec scripts/run-contract-tests.sh
fi

set --
while IFS= read -r path; do
    [ -n "$path" ] && set -- "$@" "$path"
done < <("$PY" -c 'import json,sys; [print(x) for x in json.load(open(sys.argv[1])).get("tests", [])]' "$selection")
if [ "$#" -eq 0 ]; then
    echo "sanity selection: no executable tests required for this non-code change"
    exit 0
fi
rm -f "$selection"
trap - EXIT
exec scripts/run-contract-tests.sh "$@"
