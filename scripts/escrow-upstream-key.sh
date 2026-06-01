#!/usr/bin/env bash
# Escrow the NVIDIA upstream key from a TokenHub host into a mac agent's
# encrypted secret store, so the in-mac model router can talk to the upstream
# DIRECTLY (key=secret:nvidia-upstream) instead of proxying through TokenHub.
#
# This is the operator action that crosses the credential boundary for the
# th-merge-04/07 direct cutover — deliberately a human-run step, not something
# an agent self-serves by scraping the credential store.
#
# The key is read on SRC_HOST, held only in this script's memory, pushed to a
# 0600 temp on DST_AGENT, and escrowed via DST_AGENT's LOCAL mac API (POST
# /secrets) so it is encrypted with the running API's live Fernet key and is
# immediately resolvable by the router. The value is never printed; the temp is
# removed right after use.
#
# Usage:    bash scripts/escrow-upstream-key.sh [DST_AGENT]
# Env:      SRC_HOST (default rocky), SECRET_NAME (default nvidia-upstream)
# Verify:   ssh <DST_AGENT> 'set -a; . ~/.mac/mac.env; set +a;
#             curl -s localhost:$MAC_PORT/secrets -H "Authorization: Bearer $MAC_API_TOKEN"'
set -euo pipefail

DST_AGENT="${1:-${DST_AGENT:-natasha}}"
SRC_HOST="${SRC_HOST:-rocky}"
SECRET_NAME="${SECRET_NAME:-nvidia-upstream}"
TMP_REMOTE='$HOME/.mac/.upstream-key.tmp'

echo ">> [1/3] extracting nvapi- key from ${SRC_HOST}:~/.tokenhub/credentials" >&2
KEY="$(ssh "$SRC_HOST" 'python3 - <<PY
import json, os, sys
d = json.load(open(os.path.expanduser("~/.tokenhub/credentials")))
def find(o):
    if isinstance(o, str):
        return o if o.startswith("nvapi-") else None
    if isinstance(o, dict):
        for v in o.values():
            r = find(v)
            if r:
                return r
    if isinstance(o, list):
        for v in o:
            r = find(v)
            if r:
                return r
    return None
sys.stdout.write(find(d) or "")
PY')"
[ -n "$KEY" ] || { echo "ERROR: no nvapi- key found on ${SRC_HOST}" >&2; exit 1; }

echo ">> [2/3] pushing key to ${DST_AGENT} (0600 temp, not printed)" >&2
printf '%s' "$KEY" | ssh "$DST_AGENT" "umask 077; cat > ${TMP_REMOTE}"
unset KEY

echo ">> [3/3] escrowing as '${SECRET_NAME}' via ${DST_AGENT}'s live mac API" >&2
ssh "$DST_AGENT" 'bash -s' <<REMOTE
set -euo pipefail
set -a; . "\$HOME/.mac/mac.env" >/dev/null 2>&1; set +a
SECRET_NAME='${SECRET_NAME}' TMPKEY="${TMP_REMOTE}" python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error
name = os.environ["SECRET_NAME"]
path = os.path.expanduser(os.environ["TMPKEY"])
key = open(path).read().strip()
os.remove(path)
if not key:
    print("ERROR: empty key on destination", file=sys.stderr); sys.exit(1)
port = os.environ.get("MAC_PORT", "8789")
tok = os.environ.get("MAC_API_TOKEN", "")
body = json.dumps({
    "name": name,
    "value": key,
    "scopes": {"capabilities": ["router-upstream"]},
    "created_by": "operator",
}).encode()
req = urllib.request.Request(
    "http://127.0.0.1:%s/secrets" % port, data=body,
    headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
    method="POST",
)
try:
    r = urllib.request.urlopen(req, timeout=15)
    print("escrowed secret %r (HTTP %s, %d-char value)" % (name, r.status, len(key)))
except urllib.error.HTTPError as e:
    print("escrow failed: HTTP %s %s" % (e.code, e.read().decode()[:300]), file=sys.stderr)
    sys.exit(1)
PY
REMOTE

echo ">> done — set the router provider key=secret:${SECRET_NAME} on ${DST_AGENT}" >&2
