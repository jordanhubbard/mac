#!/usr/bin/env bash
set -euo pipefail

# Establish the node-local half of the fleet phase-1 barrier.  The outer
# controller is responsible for holding and draining the complete cohort before
# invoking this helper on every selected node.  This helper deliberately never
# names or stops the MAC control-plane service.

umask 077

REQUESTED_ACTION="${1:-quiesce}"
case "$REQUESTED_ACTION" in
  identify) ACTION=identify ;;
  arm-phase1|prepare) ACTION=prepare ;;
  quiesce) ACTION=quiesce ;;
  restore-phase1|restore) ACTION=restore ;;
  *)
    printf '%s\n' \
      "usage: $0 [identify|arm-phase1|quiesce|restore-phase1]" >&2
    exit 64
    ;;
esac

phase1_die() {
  printf '%s\n' "phase-1 quiescence failed: $*" >&2
  exit 1
}

AGENT="${AGENT:-}"
FLEET_NAME="${FLEET_NAME:-${FLEET:-}}"
OS_KIND="${OS_KIND:-${OS:-}}"
DEPLOY_REV="${DEPLOY_REV:-${REVISION:-${MAC_DEPLOY_GIT_REV:-}}}"
DEPLOY_GENERATION="${DEPLOY_GENERATION:-${GENERATION:-${MAC_DEPLOY_GENERATION:-}}}"
MAC_HOME="${MAC_HOME:-}"
PY="${PY:-python3}"
DAEMON_FUNCTIONS_FILE="${MAC_PHASE1_DAEMON_FUNCTIONS_FILE:-}"
SUPERVISOR_KIND="${SUPERVISOR_KIND:-auto}"
PHASE1_HELPER_SOURCE="${MAC_PHASE1_HELPER_SOURCE:-$0}"

[ -n "$AGENT" ] || phase1_die "AGENT is required"
[ -n "$FLEET_NAME" ] || phase1_die "FLEET_NAME is required"
[ -n "$OS_KIND" ] || phase1_die "OS_KIND is required"
[ -n "$DEPLOY_REV" ] || phase1_die "DEPLOY_REV is required"
[ -n "$DEPLOY_GENERATION" ] || phase1_die "DEPLOY_GENERATION is required"
[ -n "$MAC_HOME" ] || phase1_die "MAC_HOME is required"
if [ "$ACTION" = prepare ]; then
  [ -n "$DAEMON_FUNCTIONS_FILE" ] \
    || phase1_die "MAC_PHASE1_DAEMON_FUNCTIONS_FILE is required"
elif [ "$ACTION" != identify ]; then
  # Quiescence and recovery must use the exact daemon logic retained by
  # prepare.  Never accept a newer checkout's function block for an already
  # prepared generation.
  DAEMON_FUNCTIONS_FILE="$MAC_HOME/phase1-restore-${DEPLOY_GENERATION}/daemon-functions.sh"
fi

case "$PY" in
  /*) ;;
  *) PY="$(command -v "$PY" 2>/dev/null || true)" ;;
esac
[ -n "$PY" ] && [ -x "$PY" ] || phase1_die "PY is not executable"
[ -d "$MAC_HOME" ] || phase1_die "MAC_HOME is unavailable"
if [ "$ACTION" != identify ]; then
  [ -f "$DAEMON_FUNCTIONS_FILE" ] && [ ! -L "$DAEMON_FUNCTIONS_FILE" ] \
    && [ -r "$DAEMON_FUNCTIONS_FILE" ] \
    || phase1_die "daemon quiescence function block is not a readable regular file"
fi

case "$AGENT" in
  [A-Za-z0-9]*) ;;
  *) phase1_die "AGENT is invalid" ;;
esac
case "$AGENT" in
  *[!A-Za-z0-9._-]*) phase1_die "AGENT is invalid" ;;
esac
[ "${#AGENT}" -le 128 ] || phase1_die "AGENT is invalid"
case "$FLEET_NAME" in
  [A-Za-z0-9]*) ;;
  *) phase1_die "FLEET_NAME is invalid" ;;
esac
case "$FLEET_NAME" in
  *[!A-Za-z0-9._-]*) phase1_die "FLEET_NAME is invalid" ;;
esac
[ "${#FLEET_NAME}" -le 128 ] || phase1_die "FLEET_NAME is invalid"
case "$DEPLOY_GENERATION" in
  [A-Za-z0-9]*) ;;
  *) phase1_die "DEPLOY_GENERATION is invalid" ;;
esac
case "$DEPLOY_GENERATION" in
  *[!A-Za-z0-9._:+-]*) phase1_die "DEPLOY_GENERATION is invalid" ;;
esac
[ "${#DEPLOY_GENERATION}" -le 181 ] \
  || phase1_die "DEPLOY_GENERATION is invalid"
[ "${#DEPLOY_REV}" -eq 40 ] || phase1_die "DEPLOY_REV is invalid"
case "$DEPLOY_REV" in
  *[!0-9a-f]*) phase1_die "DEPLOY_REV is invalid" ;;
esac

OS_KIND="$(printf '%s' "$OS_KIND" | tr '[:upper:]' '[:lower:]')"
SUPERVISOR_KIND="$(printf '%s' "$SUPERVISOR_KIND" | tr '[:upper:]' '[:lower:]')"

DAEMON_RECEIPT="${MAC_PHASE1_DAEMON_RECEIPT_PATH:-$MAC_HOME/daemon-resource-quiescence-${DEPLOY_GENERATION}.json}"
PHASE1_RECEIPT="${MAC_PHASE1_RECEIPT_PATH:-$MAC_HOME/phase1-cohort-quiescence-${DEPLOY_GENERATION}.json}"
RESTORE_CONTRACT="${MAC_PHASE1_RESTORE_CONTRACT_PATH:-$MAC_HOME/phase1-cohort-restore-contract-${DEPLOY_GENERATION}.json}"
RESTORE_RECEIPT="${MAC_PHASE1_RESTORE_RECEIPT_PATH:-$MAC_HOME/phase1-cohort-restore-${DEPLOY_GENERATION}.json}"
DAEMON_RESTORE_CONTRACT="$MAC_HOME/daemon-resource-restore-contract-${DEPLOY_GENERATION}.json"
DAEMON_RESTORE_RECEIPT="$MAC_HOME/daemon-resource-restore-${DEPLOY_GENERATION}.json"
RESTORE_ARTIFACT_DIR="$MAC_HOME/phase1-restore-${DEPLOY_GENERATION}"
LOCAL_RESTORE_MANIFEST="$RESTORE_ARTIFACT_DIR/local-artifacts.json"
RESTORE_EXECUTABLE="$RESTORE_ARTIFACT_DIR/phase1-restore"
RETAINED_DAEMON_FUNCTIONS="$RESTORE_ARTIFACT_DIR/daemon-functions.sh"
if [ "$ACTION" = prepare ] || [ "$ACTION" = restore ]; then
  SUPERVISOR_PROOF="$MAC_HOME/phase1-supervisor-${ACTION}-${DEPLOY_GENERATION}.json"
else
  SUPERVISOR_PROOF="$MAC_HOME/.phase1-supervisor-${ACTION}-${DEPLOY_GENERATION}.$$.json"
fi
DAEMON_FUNCTIONS_SNAPSHOT="$MAC_HOME/.phase1-daemon-functions-${DEPLOY_GENERATION}.$$.sh"

if [ "$ACTION" = identify ]; then
  AGENT="$AGENT" FLEET_NAME="$FLEET_NAME" OS_KIND="$OS_KIND" \
  DEPLOY_REV="$DEPLOY_REV" DEPLOY_GENERATION="$DEPLOY_GENERATION" \
  MAC_HOME="$MAC_HOME" SUPERVISOR_KIND="$SUPERVISOR_KIND" \
  RESTORE_CONTRACT="$RESTORE_CONTRACT" RESTORE_RECEIPT="$RESTORE_RECEIPT" \
  PHASE1_RECEIPT="$PHASE1_RECEIPT" RESTORE_EXECUTABLE="$RESTORE_EXECUTABLE" \
    "$PY" - <<'PY'
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import stat
import sys
from pathlib import Path


def optional_private(path: Path, limit: int) -> bytes | None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    except OSError:
        raise SystemExit("node identity input could not be opened safely")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > limit
        ):
            raise SystemExit("node identity input is not owner-private and bounded")
        raw = os.read(descriptor, before.st_size + 1)
        after = os.fstat(descriptor)
        if len(raw) != before.st_size or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SystemExit("node identity input changed while reading")
        return raw
    finally:
        os.close(descriptor)


def regular_directory(path: Path) -> bool:
    try:
        return stat.S_ISDIR(path.lstat().st_mode)
    except OSError:
        return False


mac_home = Path(os.environ["MAC_HOME"])
source = mac_home / "src" / "mac"
venv = mac_home / "venv"
generation = None
env_raw = optional_private(mac_home / "mac.env", 1024 * 1024)
if env_raw is not None:
    try:
        lines = env_raw.decode("utf-8").splitlines()
    except UnicodeDecodeError as exc:
        raise SystemExit("node identity environment is malformed") from exc
    matches = []
    for line in lines:
        try:
            fields = shlex.split(line, posix=True)
        except ValueError as exc:
            raise SystemExit("node identity environment is malformed") from exc
        if len(fields) == 1 and fields[0].startswith("MAC_WORKER_DEPLOY_GENERATION="):
            matches.append(fields[0].split("=", 1)[1])
    if len(matches) > 1:
        raise SystemExit("node identity environment has duplicate generation markers")
    if matches:
        generation = matches[0]
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,511}", generation) is None:
            raise SystemExit("node identity generation marker is invalid")
revision = None
revision_raw = optional_private(mac_home / "deployed-source-revision", 256)
if revision_raw is not None:
    try:
        revision = revision_raw.decode("ascii").strip() or None
    except UnicodeDecodeError as exc:
        raise SystemExit("node identity revision marker is malformed") from exc
    if revision is not None and re.fullmatch(r"[0-9a-f]{40}", revision) is None:
        raise SystemExit("node identity revision marker is invalid")
payload = {
    "schema": "mac.fleet_node_identity.v1",
    "status": "identified",
    "agent": os.environ["AGENT"],
    "fleet": os.environ["FLEET_NAME"],
    "os_kind": os.environ["OS_KIND"],
    "requested_generation": os.environ["DEPLOY_GENERATION"],
    "requested_revision": os.environ["DEPLOY_REV"],
    "current_generation": generation,
    "current_revision": revision,
    "supervisor": os.environ["SUPERVISOR_KIND"],
    "rollback_capable": regular_directory(source) and regular_directory(venv),
    "artifacts": {
        "source": {"path": str(source), "regular_directory": regular_directory(source)},
        "venv": {"path": str(venv), "regular_directory": regular_directory(venv)},
    },
    "prerequisites": {
        "python": sys.executable,
        "github_cli": shutil.which("gh"),
        "codegraph": str(mac_home / "bin" / "codegraph")
        if os.access(mac_home / "bin" / "codegraph", os.X_OK)
        else None,
    },
    "contracts": {
        "phase1_receipt": os.environ["PHASE1_RECEIPT"],
        "restore_contract": os.environ["RESTORE_CONTRACT"],
        "restore_executable": os.environ["RESTORE_EXECUTABLE"],
        "restore_receipt": os.environ["RESTORE_RECEIPT"],
    },
}
json.dump(payload, sys.stdout, sort_keys=True, separators=(",", ":"))
sys.stdout.write("\n")
PY
  exit 0
fi

cleanup_phase1_proof() {
  [ "$ACTION" = quiesce ] && rm -f "$SUPERVISOR_PROOF"
  rm -f "$DAEMON_FUNCTIONS_SNAPSHOT"
}
trap cleanup_phase1_proof EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

export AGENT FLEET_NAME OS_KIND DEPLOY_REV DEPLOY_GENERATION MAC_HOME PY ACTION
export MAC_PHASE1_SUPERVISOR_KIND="$SUPERVISOR_KIND"
export MAC_PHASE1_SUPERVISOR_PROOF_PATH="$SUPERVISOR_PROOF"
export MAC_PHASE1_RESTORE_CONTRACT_PATH="$RESTORE_CONTRACT"
export MAC_PHASE1_RESTORE_RECEIPT_PATH="$RESTORE_RECEIPT"
export MAC_PHASE1_DAEMON_RESTORE_CONTRACT_PATH="$DAEMON_RESTORE_CONTRACT"
export MAC_PHASE1_DAEMON_RESTORE_RECEIPT_PATH="$DAEMON_RESTORE_RECEIPT"
export MAC_PHASE1_RESTORE_ARTIFACT_DIR="$RESTORE_ARTIFACT_DIR"
export MAC_PHASE1_LOCAL_RESTORE_MANIFEST="$LOCAL_RESTORE_MANIFEST"
export MAC_PHASE1_RESTORE_EXECUTABLE="$RESTORE_EXECUTABLE"
export MAC_PHASE1_RETAINED_DAEMON_FUNCTIONS="$RETAINED_DAEMON_FUNCTIONS"
export MAC_PHASE1_HELPER_SOURCE="$PHASE1_HELPER_SOURCE"
export MAC_PHASE1_DAEMON_FUNCTIONS_SOURCE="$DAEMON_FUNCTIONS_FILE"
export MAC_PHASE1_DAEMON_FUNCTIONS_SNAPSHOT="$DAEMON_FUNCTIONS_SNAPSHOT"

if [ "$ACTION" = prepare ] \
    && { [ -e "$RESTORE_CONTRACT" ] || [ -L "$RESTORE_CONTRACT" ]; }; then
  "$PY" - "$RESTORE_CONTRACT" "$AGENT" "$DEPLOY_GENERATION" "$DEPLOY_REV" <<'PY'
import hashlib
import json
import os
import stat
import sys

path, agent, generation, revision = sys.argv[1:]
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > 4 * 1024 * 1024
    ):
        raise SystemExit("phase-1 prepare failed: existing restore contract is unsafe")
    raw = os.read(descriptor, metadata.st_size + 1)
finally:
    os.close(descriptor)
try:
    payload = json.loads(raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 prepare failed: existing restore contract is malformed")
if (
    payload.get("schema") != "mac.phase1_cohort_restore_contract.v1"
    or payload.get("agent") != agent
    or payload.get("generation") != generation
    or payload.get("revision") != revision
):
    raise SystemExit("phase-1 prepare failed: existing restore contract belongs to another generation")


def validate_artifact(item, mode, label):
    if (
        not isinstance(item, dict)
        or not isinstance(item.get("path"), str)
        or not os.path.isabs(item["path"])
        or not isinstance(item.get("sha256"), str)
    ):
        raise SystemExit("phase-1 prepare failed: existing %s contract is malformed" % label)
    descriptor = os.open(
        item["path"], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size <= 0
            or before.st_size > 8 * 1024 * 1024
        ):
            raise SystemExit("phase-1 prepare failed: existing %s is unsafe" % label)
        content = bytearray()
        while len(content) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(content)))
            if not chunk:
                raise SystemExit("phase-1 prepare failed: existing %s was truncated" % label)
            content.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit("phase-1 prepare failed: existing %s grew while reading" % label)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise SystemExit("phase-1 prepare failed: existing %s changed" % label)
    finally:
        os.close(descriptor)
    if hashlib.sha256(content).hexdigest() != item["sha256"]:
        raise SystemExit("phase-1 prepare failed: existing %s digest differs" % label)


validate_artifact(payload.get("restore_executable"), 0o700, "restore executable")
validate_artifact(payload.get("daemon_function_block"), 0o600, "daemon function block")
if payload["restore_executable"].get("argv") != [
    payload["restore_executable"]["path"],
    "restore",
]:
    raise SystemExit("phase-1 prepare failed: existing restore invocation is malformed")
sys.stdout.buffer.write(raw)
PY
  exit 0
fi

if [ "$ACTION" = restore ]; then
  MAC_PHASE1_EXPECTED_CONTRACT_SHA256="${MAC_PHASE1_RESTORE_CONTRACT_SHA256:-}" \
    "$PY" - "$RESTORE_CONTRACT" "$PHASE1_HELPER_SOURCE" \
      "$DAEMON_FUNCTIONS_FILE" "$AGENT" "$DEPLOY_GENERATION" "$DEPLOY_REV" <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys


contract_path, helper_source, daemon_source, agent, generation, revision = sys.argv[1:]
expected = os.environ.get("MAC_PHASE1_EXPECTED_CONTRACT_SHA256", "")
if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
    raise SystemExit("phase-1 restore failed: exact restore contract digest is required")


def private_bytes(path: str, mode: int, label: str, limit: int = 8 * 1024 * 1024) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit("phase-1 restore failed: %s is unsafe" % label)
        content = bytearray()
        while len(content) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(content)))
            if not chunk:
                raise SystemExit("phase-1 restore failed: %s was truncated" % label)
            content.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit("phase-1 restore failed: %s grew while reading" % label)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise SystemExit("phase-1 restore failed: %s changed while reading" % label)
        return bytes(content)
    finally:
        os.close(descriptor)


contract_raw = private_bytes(contract_path, 0o600, "restore contract", 4 * 1024 * 1024)
if hashlib.sha256(contract_raw).hexdigest() != expected:
    raise SystemExit("phase-1 restore failed: restore contract digest differs")
try:
    contract = json.loads(contract_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 restore failed: restore contract is malformed")
if (
    not isinstance(contract, dict)
    or contract.get("schema") != "mac.phase1_cohort_restore_contract.v1"
    or contract.get("status") != "prepared"
    or contract.get("agent") != agent
    or contract.get("generation") != generation
    or contract.get("revision") != revision
    or contract.get("rollback_capable") is not True
):
    raise SystemExit("phase-1 restore failed: restore contract belongs to another generation")
helper = contract.get("restore_executable")
daemon = contract.get("daemon_function_block")
current_helper = os.path.abspath(helper_source)
current_daemon = os.path.abspath(daemon_source)
if (
    not isinstance(helper, dict)
    or helper.get("path") != current_helper
    or helper.get("argv") != [current_helper, "restore"]
    or not isinstance(helper.get("sha256"), str)
):
    raise SystemExit("phase-1 restore failed: exact retained restore executable is required")
if (
    not isinstance(daemon, dict)
    or daemon.get("path") != current_daemon
    or not isinstance(daemon.get("sha256"), str)
):
    raise SystemExit("phase-1 restore failed: exact retained daemon function block is required")
if hashlib.sha256(private_bytes(current_helper, 0o700, "restore executable")).hexdigest() != helper["sha256"]:
    raise SystemExit("phase-1 restore failed: retained restore executable digest differs")
if hashlib.sha256(private_bytes(current_daemon, 0o600, "daemon function block")).hexdigest() != daemon["sha256"]:
    raise SystemExit("phase-1 restore failed: retained daemon function block digest differs")
PY
fi

if [ "$ACTION" = restore ] \
    && { [ -e "$RESTORE_RECEIPT" ] || [ -L "$RESTORE_RECEIPT" ]; }; then
  MAC_PHASE1_EXPECTED_CONTRACT_SHA256="${MAC_PHASE1_RESTORE_CONTRACT_SHA256:-}" \
    "$PY" - "$RESTORE_RECEIPT" "$AGENT" "$DEPLOY_GENERATION" "$DEPLOY_REV" <<'PY'
import json
import os
import re
import stat
import sys

path, agent, generation, revision = sys.argv[1:]
expected = os.environ.get("MAC_PHASE1_EXPECTED_CONTRACT_SHA256", "")
if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
    raise SystemExit("phase-1 restore failed: exact restore contract digest is required")
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
descriptor = os.open(path, flags)
try:
    metadata = os.fstat(descriptor)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) != 0o600
        or metadata.st_size <= 0
        or metadata.st_size > 4 * 1024 * 1024
    ):
        raise SystemExit("phase-1 restore failed: existing receipt is unsafe")
    raw = os.read(descriptor, metadata.st_size + 1)
finally:
    os.close(descriptor)
try:
    payload = json.loads(raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 restore failed: existing receipt is malformed")
if (
    payload.get("schema") != "mac.phase1_cohort_restore.v1"
    or payload.get("status") != "restored"
    or payload.get("agent") != agent
    or payload.get("generation") != generation
    or payload.get("revision") != revision
    or payload.get("source_contract_sha256") != expected
):
    raise SystemExit("phase-1 restore failed: existing receipt belongs to another contract")
sys.stdout.buffer.write(raw)
PY
  exit 0
fi

# Open the reviewed block without following a final symlink, require a trusted
# non-writable owner, and copy the exact bytes into the owner-private MAC
# directory.  Sourcing the snapshot closes the check/source rename race when the
# controller supplied a path in a shared staging directory.
DAEMON_FUNCTIONS_SHA256="$("$PY" - <<'PY'
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import stat
import tempfile

source = Path(os.environ["MAC_PHASE1_DAEMON_FUNCTIONS_SOURCE"])
destination = Path(os.environ["MAC_PHASE1_DAEMON_FUNCTIONS_SNAPSHOT"])
flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
try:
    descriptor = os.open(str(source), flags)
except OSError:
    raise SystemExit("phase-1 quiescence failed: daemon function block could not be opened safely")
try:
    metadata = os.fstat(descriptor)
    if not stat.S_ISREG(metadata.st_mode):
        raise SystemExit("phase-1 quiescence failed: daemon function block is not regular")
    if metadata.st_uid not in {0, os.getuid()} or metadata.st_mode & 0o022:
        raise SystemExit("phase-1 quiescence failed: daemon function block is not trusted")
    if metadata.st_size <= 0 or metadata.st_size > 2 * 1024 * 1024:
        raise SystemExit("phase-1 quiescence failed: daemon function block violates its size bound")
    chunks = []
    remaining = metadata.st_size
    while remaining:
        chunk = os.read(descriptor, min(remaining, 64 * 1024))
        if not chunk:
            raise SystemExit("phase-1 quiescence failed: daemon function block was truncated")
        chunks.append(chunk)
        remaining -= len(chunk)
    if os.read(descriptor, 1):
        raise SystemExit("phase-1 quiescence failed: daemon function block changed while reading")
    after = os.fstat(descriptor)
    if (
        after.st_dev != metadata.st_dev
        or after.st_ino != metadata.st_ino
        or after.st_size != metadata.st_size
        or after.st_mtime_ns != metadata.st_mtime_ns
        or after.st_ctime_ns != metadata.st_ctime_ns
    ):
        raise SystemExit("phase-1 quiescence failed: daemon function block changed while reading")
    raw = b"".join(chunks)
finally:
    os.close(descriptor)
if b"\x00" in raw:
    raise SystemExit("phase-1 quiescence failed: daemon function block contains invalid bytes")

destination.parent.mkdir(parents=True, exist_ok=True)
output, temporary_raw = tempfile.mkstemp(
    prefix="." + destination.name + ".", dir=str(destination.parent)
)
temporary = Path(temporary_raw)
directory = None
try:
    os.fchmod(output, 0o600)
    with os.fdopen(output, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    directory = os.open(str(destination.parent), os.O_RDONLY)
    os.replace(str(temporary), str(destination))
    os.fsync(directory)
finally:
    if directory is not None:
        os.close(directory)
    try:
        temporary.unlink()
    except FileNotFoundError:
        pass
print(hashlib.sha256(raw).hexdigest())
PY
)"
[ "${#DAEMON_FUNCTIONS_SHA256}" -eq 64 ] \
  || phase1_die "daemon function block digest is invalid"
case "$DAEMON_FUNCTIONS_SHA256" in
  *[!0-9a-f]*) phase1_die "daemon function block digest is invalid" ;;
esac
export MAC_PHASE1_DAEMON_FUNCTIONS_SHA256="$DAEMON_FUNCTIONS_SHA256"

if [ "$ACTION" = prepare ]; then
  "$PY" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from pathlib import Path


mac_home = Path(os.environ["MAC_HOME"])
root = Path(os.environ["MAC_PHASE1_RESTORE_ARTIFACT_DIR"])
manifest_path = Path(os.environ["MAC_PHASE1_LOCAL_RESTORE_MANIFEST"])
if root.exists() or root.is_symlink():
    raise SystemExit("phase-1 prepare failed: local restore staging already exists")
root.mkdir(mode=0o700)


def stable_source_bytes(source: Path, limit: int, label: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(source), flags)
    except OSError as exc:
        raise SystemExit("phase-1 prepare failed: %s is unavailable" % label) from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o022
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit("phase-1 prepare failed: %s is unsafe" % label)
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("phase-1 prepare failed: %s was truncated" % label)
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise SystemExit("phase-1 prepare failed: %s grew while reading" % label)
        after = os.fstat(descriptor)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise SystemExit("phase-1 prepare failed: %s changed while reading" % label)
        return bytes(raw)
    finally:
        os.close(descriptor)


def write_all(descriptor: int, raw: bytes) -> None:
    offset = 0
    while offset < len(raw):
        written = os.write(descriptor, raw[offset:])
        if written <= 0:
            raise SystemExit("phase-1 prepare failed: retained artifact write was truncated")
        offset += written


def retain(source: Path, destination: Path, mode: int, label: str) -> dict:
    raw = stable_source_bytes(source, 8 * 1024 * 1024, label)
    descriptor = os.open(
        str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode
    )
    try:
        write_all(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {
        "path": str(destination),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


helper_source = Path(os.environ["MAC_PHASE1_HELPER_SOURCE"])
if not helper_source.is_absolute():
    helper_source = Path.cwd() / helper_source
restore_executable = retain(
    helper_source,
    Path(os.environ["MAC_PHASE1_RESTORE_EXECUTABLE"]),
    0o700,
    "phase-1 helper",
)
daemon_function_block = retain(
    Path(os.environ["MAC_PHASE1_DAEMON_FUNCTIONS_SNAPSHOT"]),
    Path(os.environ["MAC_PHASE1_RETAINED_DAEMON_FUNCTIONS"]),
    0o600,
    "daemon function block",
)


def snapshot(source: Path, name: str) -> dict:
    destination = root / name
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(str(source), flags)
    except FileNotFoundError:
        return {"path": str(source), "backup": None, "existed": False, "sha256": None}
    except OSError as exc:
        raise SystemExit("phase-1 prepare failed: local restore input is unsafe") from exc
    try:
        before = os.fstat(source_fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size > 4 * 1024 * 1024
        ):
            raise SystemExit("phase-1 prepare failed: local restore input is not private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(source_fd, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("phase-1 prepare failed: local restore input was truncated")
            raw.extend(chunk)
        after = os.fstat(source_fd)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise SystemExit("phase-1 prepare failed: local restore input changed")
    finally:
        os.close(source_fd)
    descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        write_all(descriptor, raw)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return {
        "path": str(source),
        "backup": str(destination),
        "existed": True,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


artifacts = [
    snapshot(mac_home / "mac.env", "mac.env"),
    snapshot(mac_home / "deploy-dispatch-hold.json", "deploy-dispatch-hold.json"),
]
source = mac_home / "src" / "mac"
venv = mac_home / "venv"
rollback_capable = all(
    path.is_dir() and not path.is_symlink() for path in (source, venv)
)
payload = {
    "schema": "mac.phase1_local_restore_artifacts.v1",
    "generation": os.environ["DEPLOY_GENERATION"],
    "revision": os.environ["DEPLOY_REV"],
    "rollback_capable": rollback_capable,
    "rollback_ineligible_reason": (
        None if rollback_capable else "prior source and virtualenv generation is incomplete"
    ),
    "restore_executable": restore_executable,
    "daemon_function_block": daemon_function_block,
    "artifacts": artifacts,
}
descriptor, temporary_raw = tempfile.mkstemp(prefix=".local-artifacts.", dir=str(root))
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, manifest_path)
    directory = os.open(str(root), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    parent = os.open(str(root.parent), os.O_RDONLY)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
finally:
    temporary.unlink(missing_ok=True)
PY
elif [ "$ACTION" = restore ]; then
  MAC_PHASE1_EXPECTED_CONTRACT_SHA256="${MAC_PHASE1_RESTORE_CONTRACT_SHA256:-}" \
    "$PY" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path


def private_bytes(path: Path, limit: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit("phase-1 restore failed: restore evidence is not private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("phase-1 restore failed: restore evidence was truncated")
            raw.extend(chunk)
        return bytes(raw)
    finally:
        os.close(descriptor)


contract_path = Path(os.environ["MAC_PHASE1_RESTORE_CONTRACT_PATH"])
expected = os.environ.get("MAC_PHASE1_EXPECTED_CONTRACT_SHA256", "")
if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
    raise SystemExit("phase-1 restore failed: exact restore contract digest is required")
contract_raw = private_bytes(contract_path, 4 * 1024 * 1024)
if hashlib.sha256(contract_raw).hexdigest() != expected:
    raise SystemExit("phase-1 restore failed: restore contract digest differs")
try:
    contract = json.loads(contract_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 restore failed: restore contract is malformed")
local = contract.get("local_artifacts") if isinstance(contract, dict) else None
if not isinstance(local, dict):
    raise SystemExit("phase-1 restore failed: local artifact contract is missing")
manifest_path = Path(str(local.get("path") or ""))
manifest_raw = private_bytes(manifest_path, 4 * 1024 * 1024)
if hashlib.sha256(manifest_raw).hexdigest() != local.get("sha256"):
    raise SystemExit("phase-1 restore failed: local artifact contract changed")
try:
    manifest = json.loads(manifest_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 restore failed: local artifact contract is malformed")
for item in manifest.get("artifacts") or []:
    if not isinstance(item, dict) or not isinstance(item.get("path"), str):
        raise SystemExit("phase-1 restore failed: local artifact entry is invalid")
    destination = Path(item["path"])
    if item.get("existed") is True:
        backup = Path(str(item.get("backup") or ""))
        raw = private_bytes(backup, 4 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != item.get("sha256"):
            raise SystemExit("phase-1 restore failed: local artifact snapshot changed")
        descriptor, temporary_raw = tempfile.mkstemp(
            prefix="." + destination.name + ".phase1-restore.", dir=str(destination.parent)
        )
        temporary = Path(temporary_raw)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
    elif item.get("existed") is False:
        try:
            metadata = destination.lstat()
        except FileNotFoundError:
            metadata = None
        if metadata is not None:
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise SystemExit("phase-1 restore failed: unexpected local artifact is unsafe")
            destination.unlink()
    else:
        raise SystemExit("phase-1 restore failed: local artifact existence is invalid")
    directory = os.open(str(destination.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
PY
  # Recovery must survive the controller SSH channel disappearing after the
  # local startup policy has been restored but before services are restarted.
  trap '' HUP INT TERM
fi

# Supervisor operations live in one bounded Python process.  Every external
# command has its own timeout and also shares one monotonic total deadline.  No
# raw manager output is copied into logs or durable evidence.
"$PY" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import stat
import subprocess
import tempfile
import time


class QuiescenceFailure(RuntimeError):
    pass


def bounded_number(name: str, default: float, minimum: float, maximum: float) -> float:
    raw = str(os.environ.get(name) or default)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise QuiescenceFailure("invalid phase-1 timeout configuration")
    if value < minimum or value > maximum:
        raise QuiescenceFailure("phase-1 timeout configuration is outside its bound")
    return value


agent = os.environ["AGENT"]
fleet = os.environ["FLEET_NAME"]
os_kind = os.environ["OS_KIND"]
revision = os.environ["DEPLOY_REV"]
generation = os.environ["DEPLOY_GENERATION"]
requested_manager = os.environ["MAC_PHASE1_SUPERVISOR_KIND"]
proof_path = Path(os.environ["MAC_PHASE1_SUPERVISOR_PROOF_PATH"])
action = os.environ["ACTION"]
restore_contract_path = Path(os.environ["MAC_PHASE1_RESTORE_CONTRACT_PATH"])

safe_identity = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,180}\Z")
safe_fleet = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
if not safe_identity.fullmatch(generation):
    raise SystemExit("phase-1 quiescence failed: deployment generation is invalid")
if not safe_fleet.fullmatch(fleet):
    raise SystemExit("phase-1 quiescence failed: fleet name is invalid")
if os_kind not in {"darwin", "linux"}:
    raise SystemExit("phase-1 quiescence failed: unsupported OS kind")

command_timeout = bounded_number(
    "MAC_PHASE1_COMMAND_TIMEOUT_SECONDS", 240.0, 0.05, 600.0
)
total_timeout = bounded_number(
    "MAC_PHASE1_TOTAL_TIMEOUT_SECONDS", 600.0, 0.1, 1800.0
)
poll_seconds = bounded_number(
    "MAC_PHASE1_POLL_SECONDS", 0.5, 0.01, 10.0
)
deadline = time.monotonic() + total_timeout

trusted_command_path = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
manager_command_path = trusted_command_path
test_manager_bin = os.environ.get("FAKE_MANAGER_BIN_DIR", "")
if os.environ.get("MAC_PHASE1_TEST_MODE") == "1" and test_manager_bin:
    test_manager_path = Path(test_manager_bin)
    if not test_manager_path.is_absolute() or not test_manager_path.is_dir():
        raise QuiescenceFailure("invalid phase-1 test manager directory")
    manager_command_path = str(test_manager_path) + ":" + trusted_command_path

clean_env = {
    key: value
    for key in ("HOME", "USER", "LOGNAME", "TMPDIR", "SHELL")
    if (value := os.environ.get(key))
}
clean_env["PATH"] = manager_command_path
clean_env["LC_ALL"] = "C"
clean_env["LANG"] = "C"
# The behavioral contract uses isolated fake managers. Their state selectors
# are explicitly namespaced and admitted only under the test switch; production
# manager subprocesses never inherit deploy credentials, tokens, askpass hooks,
# Python injection variables, or the ambient agent environment.
if os.environ.get("MAC_PHASE1_TEST_MODE") == "1":
    clean_env.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.startswith("FAKE_")
        }
    )


OUTPUT_LIMIT_BYTES = 128 * 1024
STREAM_LIMIT_BYTES = OUTPUT_LIMIT_BYTES // 2
STREAM_KERNEL_LIMIT_BYTES = STREAM_LIMIT_BYTES + 1


def _limit_manager_output_files() -> None:
    # stdout/stderr are anonymous regular files.  RLIMIT_FSIZE is inherited by
    # every sudo/manager descendant, so the kernel stops an output flood while
    # the command is running rather than after communicate() has accumulated it.
    resource.setrlimit(
        resource.RLIMIT_FSIZE,
        (STREAM_KERNEL_LIMIT_BYTES, STREAM_KERNEL_LIMIT_BYTES),
    )


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.25)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=0.25)
    except subprocess.TimeoutExpired:
        # The child is now unambiguously unusable.  Do not turn cleanup into a
        # second unbounded wait; the group has already received SIGKILL.
        pass


def run_bounded(
    argv: list[str], environment=None
) -> subprocess.CompletedProcess[str]:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise QuiescenceFailure("phase-1 supervisor deadline expired")
    try:
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            process = subprocess.Popen(
                argv,
                stdout=stdout_file,
                stderr=stderr_file,
                env=environment if environment is not None else clean_env,
                start_new_session=True,
                preexec_fn=_limit_manager_output_files,
            )
            try:
                process.wait(timeout=min(command_timeout, remaining))
            except subprocess.TimeoutExpired:
                # Every manager command owns a fresh process group so a timeout
                # cannot strand sudo helpers or manager descendants.
                _terminate_process_group(process)
                raise QuiescenceFailure("phase-1 supervisor command timed out")
            stdout_size = os.fstat(stdout_file.fileno()).st_size
            stderr_size = os.fstat(stderr_file.fileno()).st_size
            if (
                stdout_size > STREAM_LIMIT_BYTES
                or stderr_size > STREAM_LIMIT_BYTES
                or stdout_size + stderr_size > OUTPUT_LIMIT_BYTES
            ):
                raise QuiescenceFailure(
                    "phase-1 supervisor output exceeded its bound"
                )
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout = stdout_file.read().decode("utf-8", errors="replace")
            stderr = stderr_file.read().decode("utf-8", errors="replace")
            completed = subprocess.CompletedProcess(
                argv, process.returncode, stdout, stderr
            )
    except OSError:
        raise QuiescenceFailure("phase-1 supervisor command could not execute")
    return completed


def pause() -> None:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise QuiescenceFailure("phase-1 supervisor deadline expired")
    time.sleep(min(poll_seconds, remaining))


service_names = [
    "%s-agent.service" % fleet,
    "%s-hermes-gateway.service" % fleet,
    "%s-openclaw-gateway.service" % fleet,
    "%s-nemoclaw-gateway.service" % fleet,
]
media_service_names = [
    "%s-gen-server.service" % fleet,
    "%s-gen-audio-server.service" % fleet,
    "%s-gen-video-server.service" % fleet,
]
program_names = [name.removesuffix(".service") for name in service_names]
launchd_labels = [
    "com.%s.agent" % fleet,
    "com.%s.hermes-gateway" % fleet,
    "com.%s.openclaw-gateway" % fleet,
    "com.%s.nemoclaw-gateway" % fleet,
]

for forbidden in (
    "%s.service" % fleet,
    "com.%s.control-plane" % fleet,
    "%s-control-plane" % fleet,
):
    if forbidden in service_names or forbidden in program_names or forbidden in launchd_labels:
        raise QuiescenceFailure("control-plane identity entered the phase-1 stop set")


def command_path(name: str) -> str:
    value = shutil.which(name, path=manager_command_path)
    if not value:
        raise QuiescenceFailure("required supervisor command is unavailable")
    return value


def privileged(command: str) -> list[str]:
    if os.geteuid() == 0:
        return [command]
    sudo = shutil.which("sudo", path=manager_command_path)
    if not sudo:
        raise QuiescenceFailure("non-interactive supervisor privilege is unavailable")
    return [sudo, "-n", command]


def atomic_private_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary = Path(raw)
    directory = None
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(str(path.parent), os.O_RDONLY)
        os.replace(str(temporary), str(path))
        published = True
        os.fsync(directory)
    except Exception:
        if published:
            try:
                path.unlink()
                if directory is not None:
                    os.fsync(directory)
            except FileNotFoundError:
                pass
        raise
    finally:
        if directory is not None:
            os.close(directory)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def systemd_state(prefix: list[str], systemctl: str, unit: str) -> str:
    result = run_bounded(
        prefix
        + [
            systemctl,
            "show",
            "--no-pager",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=SubState",
            "--property=MainPID",
            unit,
        ]
    )
    if result.returncode != 0:
        raise QuiescenceFailure("systemd service inspection failed")
    values: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if not line or "=" not in line:
            raise QuiescenceFailure("systemd service inspection was malformed")
        key, value = line.split("=", 1)
        if key in values:
            raise QuiescenceFailure("systemd service inspection was ambiguous")
        values[key] = value
    if set(values) != {"LoadState", "ActiveState", "SubState", "MainPID"}:
        raise QuiescenceFailure("systemd service inspection was incomplete")
    if values["LoadState"] == "not-found":
        return "absent"
    if values["ActiveState"] in {"inactive", "failed"}:
        if values["MainPID"] != "0":
            raise QuiescenceFailure("systemd reported an inactive service with a live process")
        return "inactive"
    if values["LoadState"] not in {"loaded", "masked"}:
        raise QuiescenceFailure("systemd service has an unexpected load state")
    return "active"


def systemd_enabled_state(prefix: list[str], systemctl: str, unit: str) -> str:
    result = run_bounded(prefix + [systemctl, "is-enabled", unit])
    value = (result.stdout or "").strip()
    allowed = {"enabled", "disabled", "masked", "static", "indirect", "not-found"}
    if value not in allowed:
        raise QuiescenceFailure("systemd enablement inspection failed")
    if result.returncode not in {0, 1, 3, 4}:
        raise QuiescenceFailure("systemd enablement inspection returned an unexpected status")
    return value


def inspect_systemd() -> tuple[dict[str, object], list[str], str, list[str]]:
    systemctl = command_path("systemctl")
    prefix = privileged(systemctl)[:-1]
    prior_states = [
        (unit, systemd_state(prefix, systemctl, unit)) for unit in service_names
    ]
    media_states = [
        (unit, systemd_state(prefix, systemctl, unit))
        for unit in media_service_names
    ]
    if any(state != "absent" for _unit, state in media_states):
        raise QuiescenceFailure(
            "media-gen service is ineligible until its lifecycle joins the rollback journal"
        )
    if prior_states[-1][1] == "active":
        raise QuiescenceFailure(
            "active Nemo gateway cannot be restored without a durable runtime checkpoint"
        )
    resources = [
        {
            "name": unit,
            "prior_state": state,
            "state": state,
            "enabled_state": systemd_enabled_state(prefix, systemctl, unit),
        }
        for unit, state in prior_states
    ]
    return (
        {
            "manager": "systemd",
            "resources": resources,
            "media_resources": [
                {"name": unit, "prior_state": state, "state": state}
                for unit, state in media_states
            ],
        },
        prefix,
        systemctl,
        [state for _unit, state in prior_states],
    )


def quiesce_systemd() -> dict[str, object]:
    supervisor, prefix, systemctl, prior_values = inspect_systemd()
    resources = supervisor["resources"]
    assert isinstance(resources, list)
    for resource, prior_state in zip(resources, prior_values):
        unit = str(resource["name"])
        state = prior_state
        if state == "active":
            # A racing stop may return nonzero after the process is already gone;
            # only the exact follow-up state is authoritative.
            run_bounded(prefix + [systemctl, "stop", unit])
            while True:
                state = systemd_state(prefix, systemctl, unit)
                if state in {"absent", "inactive"}:
                    break
                pause()
        resource["state"] = state
    return supervisor


def restore_systemd(expected: dict[str, object]) -> dict[str, object]:
    systemctl = command_path("systemctl")
    prefix = privileged(systemctl)[:-1]
    resources = expected.get("resources")
    if not isinstance(resources, list):
        raise QuiescenceFailure("restore contract lacks systemd resources")
    if run_bounded(prefix + [systemctl, "daemon-reload"]).returncode != 0:
        raise QuiescenceFailure("systemd definition reload failed during restore")
    for resource in resources:
        if not isinstance(resource, dict):
            raise QuiescenceFailure("restore contract has an invalid systemd resource")
        unit = str(resource.get("name") or "")
        prior = resource.get("prior_state")
        enabled = resource.get("enabled_state")
        if enabled not in {
            "enabled",
            "disabled",
            "masked",
            "static",
            "indirect",
            "not-found",
        }:
            raise QuiescenceFailure(
                "restore contract has an invalid systemd enablement state"
            )
        # A successor may have changed the persistent enablement links even
        # when the old process topology was successfully quiesced.  Clear a
        # successor mask before reconstructing activity; reapply the prior
        # mask only after an active-but-masked service has been restarted.
        if run_bounded(prefix + [systemctl, "unmask", unit]).returncode != 0:
            raise QuiescenceFailure("restoring systemd mask intent failed")
        if prior == "active":
            if run_bounded(prefix + [systemctl, "start", unit]).returncode != 0:
                raise QuiescenceFailure("restoring active systemd service failed")
        elif prior in {"inactive", "absent"}:
            if systemd_state(prefix, systemctl, unit) == "active":
                if run_bounded(prefix + [systemctl, "stop", unit]).returncode != 0:
                    raise QuiescenceFailure("restoring inactive systemd service failed")
        else:
            raise QuiescenceFailure("restore contract has an invalid systemd prior state")
        if enabled == "enabled":
            if run_bounded(prefix + [systemctl, "enable", unit]).returncode != 0:
                raise QuiescenceFailure("restoring enabled systemd intent failed")
        elif enabled == "disabled":
            if run_bounded(prefix + [systemctl, "disable", unit]).returncode != 0:
                raise QuiescenceFailure("restoring disabled systemd intent failed")
        elif enabled == "masked":
            if run_bounded(prefix + [systemctl, "mask", unit]).returncode != 0:
                raise QuiescenceFailure("restoring masked systemd intent failed")
        # static, indirect, and not-found are definition-derived states.  The
        # restored prior source/config plus daemon-reload must reproduce them;
        # the exact post-restore inspection below rejects any drift.
    restored, _prefix, _systemctl, _states = inspect_systemd()
    restored_resources = restored.get("resources")
    assert isinstance(restored_resources, list)
    expected_by_name = {str(item["name"]): item for item in resources if isinstance(item, dict)}
    for item in restored_resources:
        expected_item = expected_by_name.get(str(item["name"]))
        if expected_item is None:
            raise QuiescenceFailure("restored systemd topology has an unexpected identity")
        if (
            item.get("prior_state") != expected_item.get("prior_state")
            or item.get("enabled_state") != expected_item.get("enabled_state")
        ):
            raise QuiescenceFailure("restored systemd topology differs from its contract")
        item["state"] = item["prior_state"]
    return restored


def launchd_state(prefix: list[str], launchctl: str, target: str) -> str:
    result = run_bounded(prefix + [launchctl, "print", target])
    if result.returncode == 0:
        return "active"
    combined = (result.stdout or "") + "\n" + (result.stderr or "")
    if result.returncode == 113 and "Could not find service" in combined:
        return "absent"
    raise QuiescenceFailure("launchd job inspection failed")


def launchd_disabled_overrides(
    prefix: list[str], launchctl: str, domain: str
) -> dict[str, bool]:
    result = run_bounded(prefix + [launchctl, "print-disabled", domain])
    if result.returncode != 0:
        raise QuiescenceFailure("launchd disable-override inspection failed")
    text = result.stdout or ""
    parsed: dict[str, bool] = {}
    pattern = re.compile(
        r'^\s*"([A-Za-z0-9._-]+)"\s*=>\s*'
        r'(enabled|disabled|true|false)\s*$'
    )
    for line in text.splitlines():
        match = pattern.fullmatch(line)
        if match is None:
            continue
        label, value = match.groups()
        if label in launchd_labels:
            if label in parsed:
                raise QuiescenceFailure(
                    "launchd disable-override inspection was ambiguous"
                )
            parsed[label] = value in {"disabled", "true"}
    for label in launchd_labels:
        if label in text and label not in parsed:
            raise QuiescenceFailure(
                "launchd disable-override inspection was malformed"
            )
    return {label: parsed.get(label, False) for label in launchd_labels}


def stop_launchd_target(
    prefix: list[str],
    launchctl: str,
    target: str,
    label: str,
    prior_state: str,
    disabled_override: bool,
) -> dict[str, object]:
    state = prior_state
    if state == "active":
        run_bounded(prefix + [launchctl, "bootout", target])
        while True:
            state = launchd_state(prefix, launchctl, target)
            if state == "absent":
                break
            pause()
    return {
        "name": label,
        "target": target,
        "prior_state": prior_state,
        "state": state,
        "disabled_override": disabled_override,
    }


def inspect_launchd() -> tuple[
    dict[str, object], list[tuple[list[str], str, str, str, bool]], str
]:
    launchctl = command_path("launchctl")
    sudo = shutil.which("sudo", path=manager_command_path)
    system_prefix: list[str]
    if os.geteuid() == 0:
        system_prefix = []
    elif sudo:
        system_prefix = [sudo, "-n"]
    else:
        raise QuiescenceFailure("non-interactive launchd privilege is unavailable")
    uid = os.getuid()
    gui_domain = "gui/%d" % uid
    gui_disabled = launchd_disabled_overrides([], launchctl, gui_domain)
    system_disabled = launchd_disabled_overrides(
        system_prefix, launchctl, "system"
    )
    targets: list[tuple[list[str], str, str, str, bool]] = []
    for label in launchd_labels:
        gui_target = "%s/%s" % (gui_domain, label)
        system_target = "system/%s" % label
        targets.append(
            (
                [],
                gui_target,
                label,
                launchd_state([], launchctl, gui_target),
                gui_disabled[label],
            )
        )
        targets.append(
            (
                system_prefix,
                system_target,
                label,
                launchd_state(system_prefix, launchctl, system_target),
                system_disabled[label],
            )
        )
    if any(
        label == launchd_labels[-1] and prior_state == "active"
        for _prefix, _target, label, prior_state, _disabled in targets
    ):
        raise QuiescenceFailure(
            "active Nemo gateway cannot be restored without a durable runtime checkpoint"
        )
    return (
        {
            "manager": "launchd",
            "resources": [
                {
                    "name": label,
                    "target": target,
                    "prior_state": prior_state,
                    "state": prior_state,
                    "disabled_override": disabled_override,
                }
                for _prefix, target, label, prior_state, disabled_override in targets
            ],
        },
        targets,
        launchctl,
    )


def quiesce_launchd() -> dict[str, object]:
    supervisor, targets, launchctl = inspect_launchd()
    resources: list[dict[str, object]] = []
    for prefix, target, label, prior_state, disabled_override in targets:
        resources.append(
            stop_launchd_target(
                prefix,
                launchctl,
                target,
                label,
                prior_state,
                disabled_override,
            )
        )
    supervisor["resources"] = resources
    return supervisor


def restore_launchd(expected: dict[str, object]) -> dict[str, object]:
    launchctl = command_path("launchctl")
    sudo = shutil.which("sudo", path=manager_command_path)
    uid = os.getuid()
    resources = expected.get("resources")
    if not isinstance(resources, list):
        raise QuiescenceFailure("restore contract lacks launchd resources")
    for resource in resources:
        if not isinstance(resource, dict):
            raise QuiescenceFailure("restore contract has an invalid launchd resource")
        label = str(resource.get("name") or "")
        target = str(resource.get("target") or "")
        prior = resource.get("prior_state")
        disabled_override = resource.get("disabled_override")
        if not isinstance(disabled_override, bool):
            raise QuiescenceFailure(
                "restore contract lacks launchd disable-override intent"
            )
        if target == "system/" + label:
            prefix = [] if os.geteuid() == 0 else ([sudo, "-n"] if sudo else [])
            if os.geteuid() != 0 and not prefix:
                raise QuiescenceFailure("non-interactive launchd privilege is unavailable")
            domain = "system"
            plist = "/Library/LaunchDaemons/%s.plist" % label
        elif target == "gui/%d/%s" % (uid, label):
            prefix = []
            domain = "gui/%d" % uid
            plist = str(Path.home() / "Library" / "LaunchAgents" / (label + ".plist"))
        else:
            raise QuiescenceFailure("restore contract has an invalid launchd target")
        current = launchd_state(prefix, launchctl, target)
        if prior == "active" and current != "active":
            if run_bounded(prefix + [launchctl, "enable", target]).returncode != 0:
                raise QuiescenceFailure("enabling launchd job for restore failed")
            result = run_bounded(prefix + [launchctl, "bootstrap", domain, plist])
            if result.returncode != 0:
                raise QuiescenceFailure("restoring launchd job failed")
        elif prior == "absent" and current == "active":
            if run_bounded(prefix + [launchctl, "bootout", target]).returncode != 0:
                raise QuiescenceFailure("removing restored launchd job failed")
        elif prior not in {"active", "absent"}:
            raise QuiescenceFailure("restore contract has an invalid launchd prior state")
        override_action = "disable" if disabled_override else "enable"
        if run_bounded(prefix + [launchctl, override_action, target]).returncode != 0:
            raise QuiescenceFailure("restoring launchd disable override failed")
    restored, _targets, _launchctl = inspect_launchd()
    expected_by_target = {
        str(item["target"]): item for item in resources if isinstance(item, dict)
    }
    restored_resources = restored.get("resources")
    assert isinstance(restored_resources, list)
    for item in restored_resources:
        expected_item = expected_by_target.get(str(item["target"]))
        if (
            expected_item is None
            or item.get("prior_state") != expected_item.get("prior_state")
            or item.get("disabled_override")
            != expected_item.get("disabled_override")
        ):
            raise QuiescenceFailure("restored launchd topology differs from its contract")
        item["state"] = item["prior_state"]
    return restored


SUPERVISOR_STATES = {
    "STOPPED",
    "STARTING",
    "RUNNING",
    "BACKOFF",
    "STOPPING",
    "EXITED",
    "FATAL",
    "UNKNOWN",
}
SUPERVISOR_INACTIVE = {"STOPPED", "EXITED", "FATAL"}


def supervisor_manager_pid(argv: list[str]) -> str | None:
    result = run_bounded(argv + ["pid"])
    text = (result.stdout or "").strip()
    if result.returncode != 0 or re.fullmatch(r"[1-9][0-9]*", text) is None:
        return None
    return text


def supervisor_program_state(argv: list[str], program: str) -> str:
    result = run_bounded(argv + ["status", program])
    lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
    if not lines:
        lines = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()]
    if len(lines) != 1:
        raise QuiescenceFailure("supervisord program inspection was malformed")
    line = lines[0]
    if re.fullmatch(re.escape(program) + r":\s+ERROR\s+\(no such process\)", line):
        if result.returncode == 0:
            raise QuiescenceFailure("supervisord missing-program result was inconsistent")
        return "absent"
    match = re.fullmatch(re.escape(program) + r"\s+([A-Z]+)(?:\s+.*)?", line)
    if match is None or match.group(1) not in SUPERVISOR_STATES:
        raise QuiescenceFailure("supervisord program inspection was unrecognized")
    if result.returncode not in {0, 3}:
        raise QuiescenceFailure("supervisord program inspection returned an unexpected status")
    return match.group(1)


def supervisor_candidates() -> list[tuple[str, str, list[str]]]:
    supervisorctl = command_path("supervisorctl")
    # The node installer, readiness proof, and rollback protocol all address
    # system supervisord. Probe that exact privilege boundary first and require
    # it to exist. A distinct unprivileged manager is still quiesced, but can
    # never be mistaken for the canonical generation-restoration target.
    system_argv = privileged(supervisorctl)
    candidates: list[tuple[str, list[str]]] = [("system", system_argv)]
    if os.geteuid() != 0:
        candidates.append(("user", [supervisorctl]))
    usable: list[tuple[str, list[str], str]] = []
    for scope, argv in candidates:
        pid = supervisor_manager_pid(argv)
        if pid is not None:
            usable.append((scope, argv, pid))
    if not any(scope == "system" for scope, _argv, _pid in usable):
        raise QuiescenceFailure("system supervisord manager could not be inspected")
    deduped: list[tuple[str, str, list[str]]] = []
    seen: set[str] = set()
    for scope, argv, pid in usable:
        if pid in seen:
            continue
        seen.add(pid)
        identity = hashlib.sha256(("supervisord-pid:" + pid).encode()).hexdigest()
        deduped.append((identity, scope, argv))
    return deduped


def inspect_supervisord() -> tuple[
    dict[str, object],
    list[tuple[str, str, list[str], list[tuple[str, str]]]],
]:
    candidates = supervisor_candidates()
    prior_managers: list[
        tuple[str, str, list[str], list[tuple[str, str]]]
    ] = []
    for identity, scope, argv in candidates:
        prior_managers.append(
            (
                identity,
                scope,
                argv,
                [
                    (program, supervisor_program_state(argv, program))
                    for program in program_names
                ],
            )
        )
    if any(
        program == program_names[-1]
        and state not in SUPERVISOR_INACTIVE | {"absent"}
        for _identity, _scope, _argv, states in prior_managers
        for program, state in states
    ):
        raise QuiescenceFailure(
            "active Nemo gateway cannot be restored without a durable runtime checkpoint"
        )
    return (
        {
            "manager": "supervisord",
            "managers": [
                {
                    "manager_identity_sha256": identity,
                    "scope": scope,
                    "resources": [
                        {"name": program, "prior_state": state, "state": state}
                        for program, state in states
                    ],
                }
                for identity, scope, _argv, states in prior_managers
            ],
        },
        prior_managers,
    )


def quiesce_supervisord() -> dict[str, object]:
    supervisor, prior_managers = inspect_supervisord()
    managers: list[dict[str, object]] = []
    for identity, scope, argv, prior_states in prior_managers:
        resources: list[dict[str, str]] = []
        for program, prior_state in prior_states:
            state = prior_state
            if state not in {"STOPPED", "absent"}:
                run_bounded(argv + ["stop", program])
                stable_inactive = 0
                while True:
                    state = supervisor_program_state(argv, program)
                    if state in {"STOPPED", "absent"}:
                        break
                    if state in {"EXITED", "FATAL"}:
                        stable_inactive += 1
                        if stable_inactive >= 2:
                            break
                    else:
                        stable_inactive = 0
                    pause()
            resources.append(
                {"name": program, "prior_state": prior_state, "state": state}
            )
        managers.append(
            {
                "manager_identity_sha256": identity,
                "scope": scope,
                "resources": resources,
            }
        )
    supervisor["managers"] = managers
    return supervisor


def restore_supervisord(expected: dict[str, object]) -> dict[str, object]:
    _current, candidates = inspect_supervisord()
    expected_managers = expected.get("managers")
    if not isinstance(expected_managers, list):
        raise QuiescenceFailure("restore contract lacks supervisord managers")
    argv_by_identity = {identity: argv for identity, _scope, argv, _states in candidates}
    for manager in expected_managers:
        if not isinstance(manager, dict):
            raise QuiescenceFailure("restore contract has an invalid supervisord manager")
        identity = str(manager.get("manager_identity_sha256") or "")
        argv = argv_by_identity.get(identity)
        if argv is None:
            raise QuiescenceFailure("supervisord manager identity changed before restore")
        resources = manager.get("resources")
        if not isinstance(resources, list):
            raise QuiescenceFailure("restore contract lacks supervisord resources")
        for resource in resources:
            if not isinstance(resource, dict):
                raise QuiescenceFailure("restore contract has an invalid supervisord resource")
            program = str(resource.get("name") or "")
            prior = resource.get("prior_state")
            current = supervisor_program_state(argv, program)
            if prior == "RUNNING" and current != "RUNNING":
                result = run_bounded(argv + ["start", program])
                if result.returncode != 0:
                    raise QuiescenceFailure("restoring supervisord program failed")
            elif prior in {"STOPPED", "absent"} and current not in {"STOPPED", "absent"}:
                run_bounded(argv + ["stop", program])
            elif prior not in {"RUNNING", "STOPPED", "absent"}:
                raise QuiescenceFailure(
                    "supervisord prior state is not exactly restorable"
                )
    restored, _candidates = inspect_supervisord()
    expected_by_identity = {
        str(item["manager_identity_sha256"]): item
        for item in expected_managers
        if isinstance(item, dict)
    }
    restored_managers = restored.get("managers")
    assert isinstance(restored_managers, list)
    for manager in restored_managers:
        expected_manager = expected_by_identity.get(str(manager["manager_identity_sha256"]))
        if expected_manager is None:
            raise QuiescenceFailure("restored supervisord manager is unexpected")
        expected_resources = {
            str(item["name"]): item
            for item in expected_manager.get("resources", [])
            if isinstance(item, dict)
        }
        resources = manager.get("resources")
        assert isinstance(resources, list)
        for item in resources:
            expected_item = expected_resources.get(str(item["name"]))
            if expected_item is None or item.get("prior_state") != expected_item.get("prior_state"):
                raise QuiescenceFailure("restored supervisord topology differs from its contract")
            item["state"] = item["prior_state"]
    return restored


def reject_unjournaled_openclaw_host_automation(manager: str) -> None:
    """Fail before service mutation when exact scheduler restore is unavailable."""
    home = Path.home()
    escaped_fleet = re.escape(fleet)
    if manager == "systemd":
        locations = [
            (
                home / ".config" / "systemd" / "user",
                re.compile(
                    r"%s-openclaw-script-[a-z0-9][a-z0-9-]*\.(?:service|timer)\Z"
                    % escaped_fleet
                ),
            ),
            (
                home
                / ".config"
                / "systemd"
                / "user"
                / "timers.target.wants",
                re.compile(
                    r"%s-openclaw-script-[a-z0-9][a-z0-9-]*\.timer\Z"
                    % escaped_fleet
                ),
            ),
        ]
    elif manager == "launchd":
        locations = [
            (
                home / "Library" / "LaunchAgents",
                re.compile(
                    r"com\.%s\.openclaw-script-[a-z0-9][a-z0-9-]*\.plist\Z"
                    % escaped_fleet
                ),
            )
        ]
    else:
        return
    for directory, pattern in locations:
        if not directory.exists():
            continue
        if not directory.is_dir() or directory.is_symlink():
            raise QuiescenceFailure("OpenClaw host automation directory is unsafe")
        for entry in directory.iterdir():
            if pattern.fullmatch(entry.name):
                raise QuiescenceFailure(
                    "prior OpenClaw host automation lacks an exact restore journal"
                )
    if manager == "systemd":
        runtime_raw = ""
        if os.environ.get("MAC_PHASE1_TEST_MODE") == "1":
            runtime_raw = os.environ.get("FAKE_USER_RUNTIME_DIR", "")
        runtime = Path(runtime_raw or ("/run/user/%d" % os.getuid()))
        if not runtime.exists():
            return
        metadata = runtime.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != os.getuid()
        ):
            raise QuiescenceFailure("systemd user-manager runtime is unsafe")
        user_env = dict(clean_env)
        user_env["XDG_RUNTIME_DIR"] = str(runtime)
        systemctl = command_path("systemctl")
        result = run_bounded(
            [
                systemctl,
                "--user",
                "list-units",
                "--all",
                "--no-legend",
                "--plain",
                "%s-openclaw-script-*.service" % fleet,
                "%s-openclaw-script-*.timer" % fleet,
            ],
            user_env,
        )
        if result.returncode != 0:
            raise QuiescenceFailure(
                "systemd user-manager automation inventory failed"
            )
        loaded_pattern = re.compile(
            r"%s-openclaw-script-[a-z0-9][a-z0-9-]*\.(?:service|timer)\Z"
            % escaped_fleet
        )
        for line in (result.stdout or "").splitlines():
            fields = line.split()
            if not fields or loaded_pattern.fullmatch(fields[0]) is None:
                raise QuiescenceFailure(
                    "systemd user-manager automation inventory was malformed"
                )
            raise QuiescenceFailure(
                "loaded OpenClaw host automation lacks an exact restore journal"
            )
    elif manager == "launchd":
        launchctl = command_path("launchctl")
        result = run_bounded(
            [launchctl, "print", "gui/%d" % os.getuid()]
        )
        if result.returncode not in {0, 113}:
            raise QuiescenceFailure("launchd automation inventory failed")
        loaded_pattern = re.compile(
            r"(?<![A-Za-z0-9_.-])com\.%s\.openclaw-script-"
            r"[a-z0-9][a-z0-9-]*(?![A-Za-z0-9_.-])" % escaped_fleet
        )
        if loaded_pattern.search((result.stdout or "") + "\n" + (result.stderr or "")):
            raise QuiescenceFailure(
                "loaded OpenClaw host automation lacks an exact restore journal"
            )


def private_contract() -> dict[str, object]:
    expected_sha256 = os.environ.get("MAC_PHASE1_RESTORE_CONTRACT_SHA256", "")
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise QuiescenceFailure("exact prepared restore contract digest is required")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(restore_contract_path), flags)
    except OSError:
        raise QuiescenceFailure("prepared restore contract is unavailable")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > 4 * 1024 * 1024
        ):
            raise QuiescenceFailure("prepared restore contract is not owner-private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise QuiescenceFailure("prepared restore contract was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise QuiescenceFailure("prepared restore contract grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise QuiescenceFailure("prepared restore contract changed while reading")
    finally:
        os.close(descriptor)
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise QuiescenceFailure("prepared restore contract digest does not match")
    try:
        contract = json.loads(raw)
    except (TypeError, ValueError):
        raise QuiescenceFailure("prepared restore contract is malformed")
    if (
        not isinstance(contract, dict)
        or contract.get("schema") != "mac.phase1_cohort_restore_contract.v1"
        or contract.get("agent") != agent
        or contract.get("fleet") != fleet
        or contract.get("os_kind") != os_kind
        or contract.get("generation") != generation
        or contract.get("revision") != revision
        or contract.get("rollback_capable") is not True
        or not isinstance(contract.get("supervisor"), dict)
    ):
        raise QuiescenceFailure("prepared restore contract belongs to another node generation")
    daemon_contract = contract.get("daemon_function_block")
    daemon_sha256 = os.environ.get("MAC_PHASE1_DAEMON_FUNCTIONS_SHA256", "")
    daemon_path = os.path.abspath(
        os.environ.get("MAC_PHASE1_DAEMON_FUNCTIONS_SOURCE", "")
    )
    if (
        not isinstance(daemon_contract, dict)
        or daemon_contract.get("path") != daemon_path
        or daemon_contract.get("sha256") != daemon_sha256
    ):
        raise QuiescenceFailure(
            "retained daemon function block differs from prepared contract"
        )
    return contract


def initial_topology(supervisor: dict[str, object]) -> dict[str, object]:
    value = json.loads(json.dumps(supervisor))
    if value.get("manager") == "supervisord":
        groups = value.get("managers") or []
    else:
        groups = [value]
    for group in groups:
        if not isinstance(group, dict):
            continue
        for resource in group.get("resources") or []:
            if isinstance(resource, dict):
                resource["state"] = resource.get("prior_state")
    return value


try:
    manager = requested_manager
    if manager == "auto":
        if os_kind == "darwin":
            manager = "launchd"
        elif shutil.which("systemctl", path=manager_command_path) and Path("/run/systemd/system").is_dir():
            manager = "systemd"
        elif shutil.which("supervisorctl", path=manager_command_path):
            manager = "supervisord"
        else:
            raise QuiescenceFailure("no supported supervisor was detected")
    reject_unjournaled_openclaw_host_automation(manager)
    if action == "prepare":
        if manager == "systemd":
            supervisor = inspect_systemd()[0]
        elif manager == "launchd":
            supervisor = inspect_launchd()[0]
        elif manager == "supervisord":
            supervisor = inspect_supervisord()[0]
        else:
            raise QuiescenceFailure("unsupported supervisor kind")
        proof_schema = "mac.phase1_supervisor_prepare.v1"
    else:
        contract = private_contract()
        expected_supervisor = contract["supervisor"]
        assert isinstance(expected_supervisor, dict)
        if expected_supervisor.get("manager") != manager:
            raise QuiescenceFailure("prepared supervisor manager differs from requested manager")
        if action == "quiesce":
            if manager == "systemd":
                supervisor = quiesce_systemd()
            elif manager == "launchd":
                supervisor = quiesce_launchd()
            elif manager == "supervisord":
                supervisor = quiesce_supervisord()
            else:
                raise QuiescenceFailure("unsupported supervisor kind")
            if initial_topology(supervisor) != expected_supervisor:
                raise QuiescenceFailure("supervisor topology changed after prepare")
            proof_schema = "mac.phase1_supervisor_quiescence.v1"
        elif action == "restore":
            if manager == "systemd":
                supervisor = restore_systemd(expected_supervisor)
            elif manager == "launchd":
                supervisor = restore_launchd(expected_supervisor)
            elif manager == "supervisord":
                supervisor = restore_supervisord(expected_supervisor)
            else:
                raise QuiescenceFailure("unsupported supervisor kind")
            if initial_topology(supervisor) != expected_supervisor:
                raise QuiescenceFailure("restored supervisor topology differs from its contract")
            proof_schema = "mac.phase1_supervisor_restore.v1"
        else:
            raise QuiescenceFailure("unsupported phase-1 action")
    proof = {
        "schema": proof_schema,
        "agent": agent,
        "fleet": fleet,
        "os_kind": os_kind,
        "revision": revision,
        "generation": generation,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "supervisor": supervisor,
    }
    atomic_private_json(proof_path, proof)
except QuiescenceFailure as exc:
    print("phase-1 quiescence failed: %s" % exc, file=os.sys.stderr)
    raise SystemExit(1)
except Exception as exc:
    print(
        "phase-1 quiescence failed unexpectedly: %s" % type(exc).__name__,
        file=os.sys.stderr,
    )
    raise SystemExit(1)
PY

# The reviewed production daemon block reports progress through the installer's
# log function.  This standalone phase intentionally exposes only a fixed-text
# shim: it satisfies that interface without echoing arguments or secret-bearing
# command output into deployment logs.
log() {
  printf '%s\n' 'phase-1 daemon quiescence in progress'
}

# shellcheck source=/dev/null -- exact owner-private snapshot verified above.
. "$DAEMON_FUNCTIONS_SNAPSHOT"

if [ "$ACTION" = prepare ]; then
  declare -F prepare_daemon_resources_for_phase1_restore >/dev/null 2>&1 \
    || phase1_die "daemon function block lacks the prepare-restore entrypoint"
  prepare_daemon_resources_for_phase1_restore
  "$PY" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import time
from pathlib import Path


def private_bytes(
    path: Path, limit: int = 4 * 1024 * 1024, mode: int = 0o600
) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size <= 0
            or before.st_size > limit
        ):
            raise SystemExit("phase-1 prepare failed: restore input is not private and bounded")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise SystemExit("phase-1 prepare failed: restore input was truncated")
            raw.extend(chunk)
        return bytes(raw)
    finally:
        os.close(descriptor)


supervisor_path = Path(os.environ["MAC_PHASE1_SUPERVISOR_PROOF_PATH"])
daemon_path = Path(os.environ["MAC_PHASE1_DAEMON_RESTORE_CONTRACT_PATH"])
local_path = Path(os.environ["MAC_PHASE1_LOCAL_RESTORE_MANIFEST"])
restore_executable_path = Path(os.environ["MAC_PHASE1_RESTORE_EXECUTABLE"])
retained_daemon_path = Path(os.environ["MAC_PHASE1_RETAINED_DAEMON_FUNCTIONS"])
output = Path(os.environ["MAC_PHASE1_RESTORE_CONTRACT_PATH"])
supervisor_raw = private_bytes(supervisor_path)
daemon_raw = private_bytes(daemon_path)
local_raw = private_bytes(local_path)
restore_executable_raw = private_bytes(
    restore_executable_path, 8 * 1024 * 1024, 0o700
)
retained_daemon_raw = private_bytes(retained_daemon_path, 8 * 1024 * 1024)
try:
    supervisor_proof = json.loads(supervisor_raw)
    daemon = json.loads(daemon_raw)
    local = json.loads(local_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 prepare failed: restore input is malformed")
generation = os.environ["DEPLOY_GENERATION"]
revision = os.environ["DEPLOY_REV"]
if (
    supervisor_proof.get("schema") != "mac.phase1_supervisor_prepare.v1"
    or supervisor_proof.get("generation") != generation
    or supervisor_proof.get("revision") != revision
    or daemon.get("schema") != "mac.daemon_resource_restore_contract.v1"
    or daemon.get("generation") != generation
    or daemon.get("revision") != revision
    or local.get("schema") != "mac.phase1_local_restore_artifacts.v1"
    or local.get("generation") != generation
    or local.get("revision") != revision
):
    raise SystemExit("phase-1 prepare failed: restore input belongs to another generation")
restore_executable_sha256 = hashlib.sha256(restore_executable_raw).hexdigest()
retained_daemon_sha256 = hashlib.sha256(retained_daemon_raw).hexdigest()
if local.get("restore_executable") != {
    "path": str(restore_executable_path),
    "sha256": restore_executable_sha256,
}:
    raise SystemExit("phase-1 prepare failed: retained restore executable changed")
if local.get("daemon_function_block") != {
    "path": str(retained_daemon_path),
    "sha256": retained_daemon_sha256,
}:
    raise SystemExit("phase-1 prepare failed: retained daemon function block changed")
supervisor = supervisor_proof.get("supervisor")
if not isinstance(supervisor, dict):
    raise SystemExit("phase-1 prepare failed: supervisor topology is missing")
manager = supervisor.get("manager")
openclaw_active = False
supervisord_restorable = True
if manager == "systemd":
    for item in supervisor.get("resources") or []:
        if isinstance(item, dict) and str(item.get("name")).endswith("-openclaw-gateway.service"):
            openclaw_active = item.get("prior_state") == "active"
elif manager == "launchd":
    openclaw_active = any(
        isinstance(item, dict)
        and str(item.get("name")).endswith(".openclaw-gateway")
        and item.get("prior_state") == "active"
        for item in supervisor.get("resources") or []
    )
elif manager == "supervisord":
    for manager_item in supervisor.get("managers") or []:
        if not isinstance(manager_item, dict):
            supervisord_restorable = False
            continue
        for item in manager_item.get("resources") or []:
            if not isinstance(item, dict) or item.get("prior_state") not in {
                "RUNNING",
                "STOPPED",
                "absent",
            }:
                supervisord_restorable = False
            if (
                manager_item.get("scope") == "system"
                and isinstance(item, dict)
                and str(item.get("name")).endswith("-openclaw-gateway")
            ):
                openclaw_active = item.get("prior_state") == "RUNNING"
else:
    raise SystemExit("phase-1 prepare failed: unsupported supervisor manager")
daemon_openclaw = daemon.get("openclaw")
if not isinstance(daemon_openclaw, dict):
    raise SystemExit("phase-1 prepare failed: daemon OpenClaw topology is missing")
sandbox_present = daemon_openclaw.get("prior_state") == "present"
daemon_restorable = daemon_openclaw.get("prior_state") in {
    "present",
    "absent",
    "not_managed",
} and (not sandbox_present or openclaw_active)
rollback_capable = bool(local.get("rollback_capable")) and daemon_restorable and supervisord_restorable
reasons = []
if not local.get("rollback_capable"):
    reasons.append(str(local.get("rollback_ineligible_reason") or "prior generation is incomplete"))
if not daemon_restorable:
    reasons.append("daemon runtime topology is not exactly restorable")
if not supervisord_restorable:
    reasons.append("supervisord prior state is not exactly restorable")
payload = {
    "schema": "mac.phase1_cohort_restore_contract.v1",
    "status": "prepared",
    "agent": os.environ["AGENT"],
    "fleet": os.environ["FLEET_NAME"],
    "os_kind": os.environ["OS_KIND"],
    "generation": generation,
    "revision": revision,
    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    "rollback_capable": rollback_capable,
    "rollback_ineligible_reason": "; ".join(reasons) or None,
    "restore_receipt": os.environ["MAC_PHASE1_RESTORE_RECEIPT_PATH"],
    "restore_executable": {
        "path": str(restore_executable_path),
        "sha256": restore_executable_sha256,
        "mode": "0700",
        "argv": [str(restore_executable_path), "restore"],
    },
    "daemon_function_block": {
        "path": str(retained_daemon_path),
        "sha256": retained_daemon_sha256,
        "mode": "0600",
    },
    "supervisor": supervisor,
    "supervisor_prepare_proof": {
        "path": str(supervisor_path),
        "sha256": hashlib.sha256(supervisor_raw).hexdigest(),
    },
    "daemon_restore_contract": {
        "path": str(daemon_path),
        "sha256": hashlib.sha256(daemon_raw).hexdigest(),
    },
    "local_artifacts": {
        "path": str(local_path),
        "sha256": hashlib.sha256(local_raw).hexdigest(),
    },
    "daemon_function_block_sha256": retained_daemon_sha256,
}
if output.exists() or output.is_symlink():
    raise SystemExit("phase-1 prepare failed: restore contract appeared concurrently")
descriptor, temporary_raw = tempfile.mkstemp(prefix="." + output.name + ".", dir=str(output.parent))
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    directory = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
sys.stdout.buffer.write(private_bytes(output))
PY
  exit 0
fi

if [ "$ACTION" = restore ]; then
  declare -F verify_daemon_resources_after_phase1_restore >/dev/null 2>&1 \
    || phase1_die "daemon function block lacks the restore verification entrypoint"
  rm -f "$DAEMON_RESTORE_RECEIPT"
  verify_daemon_resources_after_phase1_restore
  export MAC_PHASE1_DAEMON_RESTORE_RECEIPT_PATH="$DAEMON_RESTORE_RECEIPT"
  "$PY" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import tempfile
import time
from pathlib import Path


def private_bytes(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(str(path), flags)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_size <= 0
            or metadata.st_size > 4 * 1024 * 1024
        ):
            raise SystemExit("phase-1 restore failed: proof is not private and bounded")
        raw = os.read(descriptor, metadata.st_size + 1)
        if len(raw) != metadata.st_size:
            raise SystemExit("phase-1 restore failed: proof changed while reading")
        return raw
    finally:
        os.close(descriptor)


contract_path = Path(os.environ["MAC_PHASE1_RESTORE_CONTRACT_PATH"])
supervisor_path = Path(os.environ["MAC_PHASE1_SUPERVISOR_PROOF_PATH"])
daemon_path = Path(os.environ["MAC_PHASE1_DAEMON_RESTORE_RECEIPT_PATH"])
output = Path(os.environ["MAC_PHASE1_RESTORE_RECEIPT_PATH"])
expected = os.environ.get("MAC_PHASE1_RESTORE_CONTRACT_SHA256", "")
if re.fullmatch(r"[0-9a-f]{64}", expected) is None:
    raise SystemExit("phase-1 restore failed: exact restore contract digest is required")
contract_raw = private_bytes(contract_path)
supervisor_raw = private_bytes(supervisor_path)
daemon_raw = private_bytes(daemon_path)
if hashlib.sha256(contract_raw).hexdigest() != expected:
    raise SystemExit("phase-1 restore failed: restore contract digest differs")
try:
    contract = json.loads(contract_raw)
    supervisor = json.loads(supervisor_raw)
    daemon = json.loads(daemon_raw)
except (TypeError, ValueError):
    raise SystemExit("phase-1 restore failed: proof is malformed")
generation = os.environ["DEPLOY_GENERATION"]
revision = os.environ["DEPLOY_REV"]
if (
    contract.get("schema") != "mac.phase1_cohort_restore_contract.v1"
    or supervisor.get("schema") != "mac.phase1_supervisor_restore.v1"
    or daemon.get("schema") != "mac.daemon_resource_restore.v1"
    or any(item.get("generation") != generation for item in (contract, supervisor, daemon))
    or any(item.get("revision") != revision for item in (contract, supervisor, daemon))
    or supervisor.get("supervisor") != contract.get("supervisor")
):
    raise SystemExit("phase-1 restore failed: final topology proof differs from contract")
payload = {
    "schema": "mac.phase1_cohort_restore.v1",
    "status": "restored",
    "agent": os.environ["AGENT"],
    "fleet": os.environ["FLEET_NAME"],
    "os_kind": os.environ["OS_KIND"],
    "generation": generation,
    "revision": revision,
    "source_contract_sha256": expected,
    "final_topology_proof": {
        "path": str(supervisor_path),
        "sha256": hashlib.sha256(supervisor_raw).hexdigest(),
    },
    "daemon_restore_proof": {
        "path": str(daemon_path),
        "sha256": hashlib.sha256(daemon_raw).hexdigest(),
    },
    "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
}
if output.exists() or output.is_symlink():
    raise SystemExit("phase-1 restore failed: completion receipt appeared concurrently")
descriptor, temporary_raw = tempfile.mkstemp(prefix="." + output.name + ".", dir=str(output.parent))
temporary = Path(temporary_raw)
try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, output)
    directory = os.open(str(output.parent), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
finally:
    temporary.unlink(missing_ok=True)
sys.stdout.buffer.write(private_bytes(output))
PY
  exit 0
fi

# Require a fresh generation receipt from the reviewed daemon block.  Removing
# only this generation-specific path prevents a stale successful receipt from
# authorizing a failed or truncated invocation.
rm -f "$DAEMON_RECEIPT"
declare -F quiesce_daemon_resources_before_source_replacement >/dev/null 2>&1 \
  || phase1_die "daemon function block lacks the quiescence entrypoint"
quiesce_daemon_resources_before_source_replacement

export MAC_PHASE1_DAEMON_RECEIPT_PATH="$DAEMON_RECEIPT"
export MAC_PHASE1_RECEIPT_PATH="$PHASE1_RECEIPT"

# Validate both proofs, bind their exact bytes into one generation receipt, fsync
# it, and read it back before reporting success.
"$PY" - <<'PY'
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time


class ReceiptFailure(RuntimeError):
    pass


def no_duplicate_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ReceiptFailure("receipt contains duplicate keys")
        value[key] = item
    return value


def private_regular_bytes(path: Path) -> bytes:
    try:
        descriptor = os.open(
            str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError:
        raise ReceiptFailure("required receipt is unavailable")
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReceiptFailure("required receipt is not a regular file")
        if (
            before.st_uid != os.getuid()
            or stat.S_IMODE(before.st_mode) != 0o600
        ):
            raise ReceiptFailure("required receipt is not owner-private")
        if before.st_size <= 0 or before.st_size > 4 * 1024 * 1024:
            raise ReceiptFailure("required receipt violates its size bound")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise ReceiptFailure("required receipt was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise ReceiptFailure("required receipt grew while reading")
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise ReceiptFailure("required receipt changed while reading")
        return bytes(raw)
    except OSError:
        raise ReceiptFailure("required receipt is unreadable")
    finally:
        os.close(descriptor)


def decode(raw: bytes) -> dict:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=no_duplicate_object)
    except (UnicodeError, ValueError, TypeError):
        raise ReceiptFailure("required receipt is malformed")
    if not isinstance(value, dict):
        raise ReceiptFailure("required receipt is not an object")
    return value


def reject_secret_or_raw_output(value, path="receipt"):
    forbidden = {
        "authorization",
        "credential",
        "password",
        "secret",
        "stderr",
        "stdout",
        "token",
    }
    if isinstance(value, dict):
        for key, item in value.items():
            lowered = str(key).lower()
            if any(part in lowered for part in forbidden):
                raise ReceiptFailure("receipt contains a forbidden evidence field")
            reject_secret_or_raw_output(item, path + "." + str(key))
    elif isinstance(value, list):
        for item in value:
            reject_secret_or_raw_output(item, path)


def atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw = tempfile.mkstemp(prefix="." + path.name + ".", dir=str(path.parent))
    temporary = Path(raw)
    directory = None
    published = False
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(str(path.parent), os.O_RDONLY)
        os.replace(str(temporary), str(path))
        published = True
        os.fsync(directory)
    except Exception:
        if published:
            try:
                path.unlink()
                if directory is not None:
                    os.fsync(directory)
            except FileNotFoundError:
                pass
        raise
    finally:
        if directory is not None:
            os.close(directory)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


agent = os.environ["AGENT"]
fleet = os.environ["FLEET_NAME"]
os_kind = os.environ["OS_KIND"]
revision = os.environ["DEPLOY_REV"]
generation = os.environ["DEPLOY_GENERATION"]
daemon_path = Path(os.environ["MAC_PHASE1_DAEMON_RECEIPT_PATH"])
supervisor_path = Path(os.environ["MAC_PHASE1_SUPERVISOR_PROOF_PATH"])
output_path = Path(os.environ["MAC_PHASE1_RECEIPT_PATH"])
contract_path = Path(os.environ["MAC_PHASE1_RESTORE_CONTRACT_PATH"])
expected_contract_sha256 = os.environ.get("MAC_PHASE1_RESTORE_CONTRACT_SHA256", "")
daemon_functions_sha256 = os.environ["MAC_PHASE1_DAEMON_FUNCTIONS_SHA256"]

expected_systemd = {
    "%s-agent.service" % fleet,
    "%s-hermes-gateway.service" % fleet,
    "%s-openclaw-gateway.service" % fleet,
    "%s-nemoclaw-gateway.service" % fleet,
}
expected_programs = {name.removesuffix(".service") for name in expected_systemd}
expected_media_systemd = {
    "%s-gen-server.service" % fleet,
    "%s-gen-audio-server.service" % fleet,
    "%s-gen-video-server.service" % fleet,
}
expected_labels = {
    "com.%s.agent" % fleet,
    "com.%s.hermes-gateway" % fleet,
    "com.%s.openclaw-gateway" % fleet,
    "com.%s.nemoclaw-gateway" % fleet,
}
SUPERVISOR_ALLOWED_PRIOR_STATES = {
    "absent",
    "STOPPED",
    "STARTING",
    "RUNNING",
    "BACKOFF",
    "STOPPING",
    "EXITED",
    "FATAL",
    "UNKNOWN",
}


def exact_named_states(
    resources, expected_names, allowed_states, allowed_prior_states
):
    if not isinstance(resources, list) or len(resources) != len(expected_names):
        raise ReceiptFailure("supervisor proof has the wrong resource count")
    names = []
    for item in resources:
        if not isinstance(item, dict):
            raise ReceiptFailure("supervisor proof has an invalid resource")
        name = item.get("name")
        state = item.get("state")
        prior_state = item.get("prior_state")
        if (
            name not in expected_names
            or state not in allowed_states
            or prior_state not in allowed_prior_states
        ):
            raise ReceiptFailure("supervisor proof has an unexpected resource state")
        names.append(name)
    if len(set(names)) != len(names) or set(names) != expected_names:
        raise ReceiptFailure("supervisor proof has missing or duplicate resources")


def validate_supervisor_payload(payload):
    if not isinstance(payload, dict):
        raise ReceiptFailure("supervisor proof lacks its manager result")
    manager = payload.get("manager")
    if manager == "systemd":
        resources = payload.get("resources")
        exact_named_states(
            resources,
            expected_systemd,
            {"absent", "inactive"},
            {"absent", "inactive", "active"},
        )
        exact_named_states(
            payload.get("media_resources"),
            expected_media_systemd,
            {"absent"},
            {"absent"},
        )
        if not all(
            isinstance(item, dict)
            and item.get("enabled_state")
            in {"enabled", "disabled", "masked", "static", "indirect", "not-found"}
            for item in resources
        ):
            raise ReceiptFailure("systemd proof lacks exact enablement state")
        return
    if manager == "launchd":
        resources = payload.get("resources")
        if not isinstance(resources, list) or len(resources) != 2 * len(expected_labels):
            raise ReceiptFailure("launchd proof has the wrong resource count")
        observed_targets = set()
        for item in resources:
            if (
                not isinstance(item, dict)
                or item.get("state") != "absent"
                or item.get("prior_state") not in {"absent", "active"}
                or not isinstance(item.get("disabled_override"), bool)
            ):
                raise ReceiptFailure("launchd proof has an active or invalid job")
            label = item.get("name")
            target = item.get("target")
            if label not in expected_labels or not isinstance(target, str):
                raise ReceiptFailure("launchd proof has an unexpected job")
            if target not in {"system/" + label, "gui/%d/%s" % (os.getuid(), label)}:
                raise ReceiptFailure("launchd proof has an unexpected domain")
            if target in observed_targets:
                raise ReceiptFailure("launchd proof has duplicate jobs")
            observed_targets.add(target)
        if len(observed_targets) != 2 * len(expected_labels):
            raise ReceiptFailure("launchd proof is incomplete")
        return
    if manager == "supervisord":
        managers = payload.get("managers")
        if not isinstance(managers, list) or not managers:
            raise ReceiptFailure("supervisord proof lacks a usable manager")
        identities = set()
        scopes = []
        for item in managers:
            if not isinstance(item, dict):
                raise ReceiptFailure("supervisord proof has an invalid manager")
            identity = item.get("manager_identity_sha256")
            if not isinstance(identity, str) or re.fullmatch(r"[0-9a-f]{64}", identity) is None:
                raise ReceiptFailure("supervisord proof has an invalid manager identity")
            if identity in identities:
                raise ReceiptFailure("supervisord proof has duplicate managers")
            identities.add(identity)
            scope = item.get("scope")
            if scope not in {"system", "user"}:
                raise ReceiptFailure("supervisord proof has an invalid manager scope")
            scopes.append(scope)
            exact_named_states(
                item.get("resources"),
                expected_programs,
                {"absent", "STOPPED", "EXITED", "FATAL"},
                SUPERVISOR_ALLOWED_PRIOR_STATES,
            )
        if scopes.count("system") != 1:
            raise ReceiptFailure("supervisord proof lacks one canonical system manager")
        return
    raise ReceiptFailure("supervisor proof names an unsupported manager")

try:
    if re.fullmatch(r"[0-9a-f]{64}", expected_contract_sha256) is None:
        raise ReceiptFailure("exact prepared restore contract digest is required")
    contract_raw = private_regular_bytes(contract_path)
    if hashlib.sha256(contract_raw).hexdigest() != expected_contract_sha256:
        raise ReceiptFailure("prepared restore contract digest does not match")
    contract = decode(contract_raw)
    if (
        contract.get("schema") != "mac.phase1_cohort_restore_contract.v1"
        or contract.get("rollback_capable") is not True
        or contract.get("generation") != generation
        or contract.get("revision") != revision
    ):
        raise ReceiptFailure("prepared restore contract is not dispatchable")
    daemon_function_contract = contract.get("daemon_function_block")
    if (
        not isinstance(daemon_function_contract, dict)
        or daemon_function_contract.get("path")
        != os.path.abspath(os.environ["MAC_PHASE1_DAEMON_FUNCTIONS_SOURCE"])
        or daemon_function_contract.get("sha256") != daemon_functions_sha256
    ):
        raise ReceiptFailure("retained daemon function block differs from contract")
    daemon_raw = private_regular_bytes(daemon_path)
    daemon = decode(daemon_raw)
    if daemon.get("schema") != "mac.daemon_resource_quiescence.v1":
        raise ReceiptFailure("daemon receipt has the wrong schema")
    if daemon.get("generation") != generation or daemon.get("revision") != revision:
        raise ReceiptFailure("daemon receipt belongs to another generation")
    runtimes = daemon.get("container_runtimes")
    proofs = daemon.get("proofs")
    pre_source = proofs.get("pre_source") if isinstance(proofs, dict) else None
    if not isinstance(runtimes, list) or not isinstance(pre_source, dict):
        raise ReceiptFailure("daemon receipt lacks its runtime proof")
    if pre_source.get("container_runtimes") != runtimes:
        raise ReceiptFailure("daemon receipt runtime identities drifted")
    if pre_source.get("stable_inactive_observations") != 2:
        raise ReceiptFailure("daemon receipt lacks stable inactivity")
    if not isinstance(daemon.get("openclaw"), dict) or daemon["openclaw"].get("final_state") != "absent":
        raise ReceiptFailure("daemon receipt lacks OpenClaw absence")
    if not isinstance(daemon.get("legacy_nemoclaw"), dict) or daemon["legacy_nemoclaw"].get("final_state") != "inactive":
        raise ReceiptFailure("daemon receipt lacks Nemo inactivity")
    reject_secret_or_raw_output(daemon)

    supervisor_raw = private_regular_bytes(supervisor_path)
    supervisor = decode(supervisor_raw)
    if supervisor.get("schema") != "mac.phase1_supervisor_quiescence.v1":
        raise ReceiptFailure("supervisor proof has the wrong schema")
    for key, expected in {
        "agent": agent,
        "fleet": fleet,
        "os_kind": os_kind,
        "revision": revision,
        "generation": generation,
    }.items():
        if supervisor.get(key) != expected:
            raise ReceiptFailure("supervisor proof belongs to another generation")
    validate_supervisor_payload(supervisor.get("supervisor"))
    reject_secret_or_raw_output(supervisor)

    if re.fullmatch(r"[0-9a-f]{64}", daemon_functions_sha256) is None:
        raise ReceiptFailure("daemon function block digest is invalid")

    payload = {
        "schema": "mac.phase1_cohort_quiescence.v1",
        "agent": agent,
        "fleet": fleet,
        "os_kind": os_kind,
        "revision": revision,
        "generation": generation,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "source_contract_sha256": expected_contract_sha256,
        "supervisor": supervisor["supervisor"],
        "daemon_resource_receipt": {
            "schema": daemon["schema"],
            "path": str(daemon_path),
            "sha256": hashlib.sha256(daemon_raw).hexdigest(),
            "proof_phase": "pre_source",
            "function_block_sha256": daemon_functions_sha256,
        },
    }
    reject_secret_or_raw_output(payload)
    atomic_private_json(output_path, payload)

    published_raw = private_regular_bytes(output_path)
    published = decode(published_raw)
    if published != payload:
        raise ReceiptFailure("published phase-1 receipt failed exact readback")
except ReceiptFailure as exc:
    try:
        output_path.unlink()
    except FileNotFoundError:
        pass
    print("phase-1 quiescence failed: %s" % exc, file=os.sys.stderr)
    raise SystemExit(1)
except Exception as exc:
    try:
        output_path.unlink()
    except FileNotFoundError:
        pass
    print(
        "phase-1 quiescence failed unexpectedly: %s" % type(exc).__name__,
        file=os.sys.stderr,
    )
    raise SystemExit(1)
PY

printf 'phase-1 quiescence complete: agent=%s generation=%s receipt=%s\n' \
  "$AGENT" "$DEPLOY_GENERATION" "$PHASE1_RECEIPT"
