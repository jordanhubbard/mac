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
parsed="$(mktemp)"
trap 'rm -f "$selection" "$parsed"' EXIT
"$PY" scripts/select-sanity-tests.py "$@" >"$selection"
"$PY" - "$selection" "$parsed" <<'PY'
import json
import sys

selection_path, parsed_path = sys.argv[1], sys.argv[2]
recognised_modes = {"full", "focused"}

try:
    with open(selection_path, encoding="utf-8") as handle:
        doc = json.load(handle)
except Exception as exc:
    print(
        "run-sanity-tests.sh: unparseable selection document: %s" % exc,
        file=sys.stderr,
    )
    sys.exit(3)

if not isinstance(doc, dict):
    print(
        "run-sanity-tests.sh: selection document must be a JSON object",
        file=sys.stderr,
    )
    sys.exit(3)

schema = doc.get("schema")
if schema != "mac.sanity_selection.v1":
    print(
        "run-sanity-tests.sh: unexpected schema %r (expected mac.sanity_selection.v1)"
        % (schema,),
        file=sys.stderr,
    )
    sys.exit(3)

mode = doc.get("mode")
if mode is None or mode == "":
    print("run-sanity-tests.sh: missing mode", file=sys.stderr)
    sys.exit(3)
if mode not in recognised_modes:
    print("run-sanity-tests.sh: unknown mode %r" % (mode,), file=sys.stderr)
    sys.exit(3)

reason = doc.get("reason")
if reason is None or reason == "":
    print("run-sanity-tests.sh: missing reason", file=sys.stderr)
    sys.exit(3)

tests = doc.get("tests", [])
if not isinstance(tests, list):
    print("run-sanity-tests.sh: tests must be a list", file=sys.stderr)
    sys.exit(3)
if any(not isinstance(path, str) or not path for path in tests):
    print("run-sanity-tests.sh: tests must be non-empty strings", file=sys.stderr)
    sys.exit(3)

print("sanity selection: %s (%s)" % (mode, reason))
prov = doc.get("provenance") or {}
if prov:
    print(
        "  provenance: %d from the change, %d always_run guards"
        % (len(prov.get("impact", [])), len(prov.get("always_run", [])))
    )
for path in doc.get("changed_files", []):
    print("  changed: " + path)
always = set(prov.get("always_run", []))
for path in tests:
    print("  test: %s%s" % (path, "  [always_run]" if path in always else ""))

with open(parsed_path, "w", encoding="utf-8") as handle:
    json.dump({"mode": mode, "reason": reason, "tests": tests}, handle)
PY

mode="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["mode"])' "$parsed")"
reason="$("$PY" -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["reason"])' "$parsed")"
if [ "$mode" = "full" ]; then
    rm -f "$selection" "$parsed"
    trap - EXIT
    exec scripts/run-contract-tests.sh
fi

set --
while IFS= read -r path; do
    [ -n "$path" ] && set -- "$@" "$path"
done < <("$PY" -c 'import json,sys; [print(x) for x in json.load(open(sys.argv[1], encoding="utf-8"))["tests"]]' "$parsed")
if [ "$#" -eq 0 ]; then
    if [ "$reason" = "non_code_change" ]; then
        echo "sanity selection: no executable tests required for this non-code change"
        exit 0
    fi
    echo "sanity selection: empty focused list (reason=$reason); escalating to whole-repo gate"
    rm -f "$selection" "$parsed"
    trap - EXIT
    exec scripts/run-contract-tests.sh
fi
rm -f "$selection" "$parsed"
trap - EXIT
exec scripts/run-contract-tests.sh "$@"
