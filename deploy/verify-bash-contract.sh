#!/bin/bash
# Verify the shell contract required by MAC's OpenShell runtimes.
#
# Keep this probe inside every sandbox image and run it both while building the
# image and through a live OpenShell sandbox.  That makes a stale/wrong runtime
# image a deployment failure instead of allowing tasks or gateways to discover
# the mismatch later.

set -euo pipefail

minimum_major=5
minimum_minor=2

if (( BASH_VERSINFO[0] < minimum_major )) || {
  (( BASH_VERSINFO[0] == minimum_major )) &&
  (( BASH_VERSINFO[1] < minimum_minor ));
}; then
  printf 'ERROR: MAC requires Bash >= %d.%d; found %s\n' \
    "$minimum_major" "$minimum_minor" "$BASH_VERSION" >&2
  exit 1
fi

# Exercise the Bash features used by runtime/bootstrap scripts instead of
# treating a version string alone as sufficient evidence.  Each check emits a
# useful deployment diagnostic rather than relying on an opaque `set -e` exit.
if [[ "${BASH:-}" != /bin/bash ]]; then
  printf 'ERROR: MAC sandbox commands must run through /bin/bash; found %s\n' \
    "${BASH:-unset}" >&2
  exit 1
fi
if ! declare -A mac_bash_contract=([shell]="${BASH}") 2>/dev/null; then
  printf 'ERROR: MAC requires Bash associative-array support\n' >&2
  exit 1
fi
mac_bash_lines=()
if ! mapfile -t mac_bash_lines < <(printf '%s\n' alpha beta); then
  printf 'ERROR: MAC requires Bash mapfile and process-substitution support\n' >&2
  exit 1
fi
if [[ "${mac_bash_contract[shell]}" != /bin/bash ]] \
    || [[ "${mac_bash_lines[*]}" != "alpha beta" ]]; then
  printf 'ERROR: MAC Bash feature verification returned unexpected results\n' >&2
  exit 1
fi
if (exit 7) | (exit 0); then
  printf 'ERROR: MAC requires working Bash pipefail semantics\n' >&2
  exit 1
fi

printf 'MAC_BASH_CONTRACT_OK version=%s executable=%s\n' "$BASH_VERSION" "$BASH"
