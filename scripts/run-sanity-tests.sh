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

# The impact-map check collects the suite, whose imports require a test DSN.
# Provision it here so both collection and the contract runner below share it.
if [ -z "${MAC_TEST_PG_URL:-}" ]; then
    _pg_helper="scripts/start-test-postgres.sh"
    if [ -x "$_pg_helper" ]; then
        echo "run-sanity-tests.sh: provisioning PostgreSQL for impact-map collection" >&2
        if _pg_dsn=$("$_pg_helper"); then
            eval "$_pg_dsn"
        fi
    fi
fi

# Selection consults the committed impact map. A stale interned node id is a
# pytest usage error (exit 4), not a test failure, so regeneration is a
# prerequisite of this script -- the same shape as test-schema-migrations
# depending on postgres-schema. `make sanity-test` already ran --check.
if [ "${MAC_IMPACT_MAP_CHECKED:-0}" != "1" ]; then
    "$PY" scripts/build-test-impact-map.py --check
fi

selection="$(mktemp)"
trap 'rm -f "$selection"' EXIT
"$PY" scripts/select-sanity-tests.py "$@" >"$selection"
"$PY" - "$selection" <<'PY'
import json, sys
doc = json.load(open(sys.argv[1], encoding="utf-8"))
print("sanity selection: %s (%s)" % (doc["mode"], doc["reason"]))
# The split matters more than the total: a "focused" selection whose cost is
# almost entirely cross-cutting guards is not narrowed in any way that helps,
# and the total alone cannot say so.
prov = doc.get("provenance") or {}
if prov:
    print(
        "  provenance: %d from the change, %d always_run guards"
        % (len(prov.get("impact", [])), len(prov.get("always_run", [])))
    )
for path in doc.get("changed_files", []):
    print("  changed: " + path)
always = set(prov.get("always_run", []))
for path in doc.get("tests", []):
    print("  test: %s%s" % (path, "  [always_run]" if path in always else ""))
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
