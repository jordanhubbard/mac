#!/bin/bash
# Fail-closed runtime probe for the confinement properties MAC depends on.
# This script is uploaded into a throwaway OpenShell sandbox by the bootstrap.
set -euo pipefail

pass() { printf 'PASS %s\n' "$1"; }
fail() { printf 'FAIL %s\n' "$1" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || fail "sandbox process runs as root"
pass "non-root uid=$(id -u)"

cap_eff="$(awk '/^CapEff:/ {print $2}' /proc/self/status)"
[ -n "$cap_eff" ] && [ "$cap_eff" = "0000000000000000" ] \
  || fail "effective capabilities are not empty: ${cap_eff:-missing}"
pass "effective capabilities empty"

no_new_privs="$(awk '/^NoNewPrivs:/ {print $2}' /proc/self/status)"
[ "$no_new_privs" = "1" ] || fail "NoNewPrivs is not enabled"
pass "no-new-privileges enabled"

seccomp="$(awk '/^Seccomp:/ {print $2}' /proc/self/status)"
[ "$seccomp" = "2" ] || fail "seccomp filter mode is not active: ${seccomp:-missing}"
pass "seccomp filter active"

probe_id="$$"
touch "/sandbox/.mac-confinement-probe-$probe_id" \
  || fail "sandbox workdir is not writable"
rm -f "/sandbox/.mac-confinement-probe-$probe_id"
pass "sandbox workdir writable"

touch "/tmp/.mac-confinement-probe-$probe_id" \
  || fail "/tmp is not writable"
rm -f "/tmp/.mac-confinement-probe-$probe_id"
pass "/tmp writable"

if touch "/etc/.mac-confinement-probe-$probe_id" 2>/dev/null; then
  rm -f "/etc/.mac-confinement-probe-$probe_id"
  fail "/etc write unexpectedly succeeded"
fi
pass "/etc write denied"

if touch "/home/sandbox/.mac-confinement-probe-$probe_id" 2>/dev/null; then
  rm -f "/home/sandbox/.mac-confinement-probe-$probe_id"
  fail "unlisted home write unexpectedly succeeded"
fi
pass "unlisted home write denied"

if unshare -Ur true >/dev/null 2>&1; then
  fail "unprivileged user namespace creation unexpectedly succeeded"
fi
pass "user namespace creation denied"

if python3 - <<'PY'
import socket

socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
PY
then
  fail "raw ICMP socket unexpectedly succeeded"
fi
pass "raw socket denied"

curl --fail --silent --show-error --max-time 20 \
  https://github.com/robots.txt >/dev/null \
  || fail "allowlisted GitHub egress failed"
pass "allowlisted GitHub egress allowed"

if curl --fail --silent --show-error --max-time 15 \
    https://example.com >/dev/null 2>&1; then
  fail "unlisted proxied egress unexpectedly succeeded"
fi
pass "unlisted proxied egress denied"

if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    -u http_proxy -u https_proxy -u all_proxy \
    curl --fail --silent --show-error --max-time 10 \
      https://example.com >/dev/null 2>&1; then
  fail "unlisted direct egress unexpectedly succeeded"
fi
pass "unlisted direct egress denied"

printf 'CONFINEMENT_PROBE_OK\n'
