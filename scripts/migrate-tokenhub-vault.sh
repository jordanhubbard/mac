#!/usr/bin/env bash
# Migrate ALL secrets from TokenHub's vault into a mac agent's encrypted secret
# store (SecretsService), preserving key names. This is part of retiring the
# standalone TokenHub: its vault (the per-agent Slack credentials, keyed
# slack.<agent>.<workspace>.<kind>) moves into mac's own Fernet-encrypted store.
#
# tokenhubctl decrypts each value (the running server's vault must be unlocked);
# we re-escrow via the mac API so each lands encrypted with mac's live Fernet
# key. Values are never printed and never written to a file — they pass through
# a shell var into a single POST per secret.
#
# Usage:  bash scripts/migrate-tokenhub-vault.sh
# Env:    TOKENHUB_HOST (default hosta)   — ssh host running TokenHub + the target mac API
#         TOKENHUB_URL  (default http://100.64.1.1:8090)
#
# Idempotent-ish: a name that already exists in the mac vault reports ALREADY
# (the mac API rejects the duplicate) rather than overwriting.
set -euo pipefail

TOKENHUB_HOST="${TOKENHUB_HOST:-hosta}"
TH_URL="${TOKENHUB_URL:-http://100.64.1.1:8090}"

echo ">> migrating TokenHub vault -> mac vault on ${TOKENHUB_HOST} (values never printed)" >&2

ssh "$TOKENHUB_HOST" "TOKENHUB_URL='${TH_URL}' bash -s" <<'REMOTE'
set -euo pipefail
set -a; . "$HOME/.tokenhub/service.env" >/dev/null 2>&1 || true; . "$HOME/.tokenhub/env" >/dev/null 2>&1 || true; set +a
set -a; . "$HOME/.mac/mac.env" >/dev/null 2>&1 || true; set +a
CTL=/home/dev/.local/bin/tokenhubctl
PORT="${MAC_PORT:-8789}"
: "${MAC_API_TOKEN:?MAC_API_TOKEN missing}"

mapfile -t KEYS < <("$CTL" vault secret list 2>/dev/null | grep -vE '^\(no secrets')
echo "vault keys: ${#KEYS[@]}"
ok=0; already=0; fail=0
for k in "${KEYS[@]}"; do
  [ -z "$k" ] && continue
  v="$("$CTL" vault secret get "$k" 2>/dev/null)" || { echo "  GET-FAIL  $k"; fail=$((fail+1)); continue; }
  res="$(KEY="$k" VAL="$v" PORT="$PORT" TOK="$MAC_API_TOKEN" python3 - <<'PY'
import os, json, urllib.request, urllib.error
name = os.environ["KEY"]; val = os.environ["VAL"]
body = json.dumps({"name": name, "value": val,
                   "scopes": {"capabilities": ["slack", "tokenhub-migrated"]},
                   "created_by": "tokenhub-vault-migration"}).encode()
req = urllib.request.Request("http://127.0.0.1:%s/secrets" % os.environ["PORT"], data=body,
        headers={"Authorization": "Bearer " + os.environ["TOK"], "Content-Type": "application/json"}, method="POST")
try:
    urllib.request.urlopen(req, timeout=15); print("OK")
except urllib.error.HTTPError as e:
    detail = e.read().decode()[:120].lower()
    print("ALREADY" if (e.code in (400, 409) and ("exist" in detail or "unique" in detail or "duplicate" in detail)) else "FAIL %s %s" % (e.code, detail))
PY
)"
  case "$res" in
    OK)      ok=$((ok+1)) ;;
    ALREADY) already=$((already+1)); echo "  exists    $k" ;;
    *)       fail=$((fail+1)); echo "  ESCROW-FAIL $k -> $res" ;;
  esac
done
echo "MIGRATION: keys=${#KEYS[@]} escrowed=$ok already=$already failed=$fail"
echo "mac vault now holds: $(curl -s -m8 http://127.0.0.1:$PORT/secrets -H "Authorization: Bearer $MAC_API_TOKEN" 2>/dev/null | python3 -c 'import sys,json;d=json.load(sys.stdin);print(len(d if isinstance(d,list) else d.get("secrets",d.get("items",[]))))' 2>/dev/null) secret(s)"
REMOTE