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
  resume-media) ACTION=resume_media ;;
  *)
    printf '%s\n' \
      "usage: $0 [identify|arm-phase1|quiesce|restore-phase1|resume-media]" >&2
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
if [ "$ACTION" = prepare ] || [ "$ACTION" = restore ] \
    || [ "$ACTION" = resume_media ]; then
  SUPERVISOR_PROOF="$MAC_HOME/phase1-supervisor-${ACTION}-${DEPLOY_GENERATION}.json"
else
  SUPERVISOR_PROOF="$MAC_HOME/.phase1-supervisor-${ACTION}-${DEPLOY_GENERATION}.$$.json"
fi
DAEMON_FUNCTIONS_SNAPSHOT="$MAC_HOME/.phase1-daemon-functions-${DEPLOY_GENERATION}.$$.sh"
# Scratch proof for the supervisor compensation that undoes a quiesce whose
# downstream daemon-resource gate rejected the deployment.  It is owner-private
# and per-process so it can never be mistaken for the canonical quiescence proof.
SUPERVISOR_COMPENSATE_PROOF="$MAC_HOME/.phase1-supervisor-compensate-${DEPLOY_GENERATION}.$$.json"

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
            or before.st_nlink != 1
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
trusted_command_path = os.pathsep.join(
    (
        str(mac_home / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
        "/Applications/Docker.app/Contents/Resources/bin",
    )
)
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
# "rollback_capable" stays a literal statement about restorable artifacts and is
# never softened for a node that has nothing to restore.  A first install is a
# different question from an upgrade, so it gets its own discriminator instead:
# install_kind is "from_scratch" only when this node carries no prior generation
# marker, no deployed revision, and neither the source tree nor the virtualenv.
# Every other node is an "upgrade", including a half-installed one -- an
# incomplete prior generation is exactly the case that must not be mistaken for
# a pristine host and silently overwritten.
rollback_capable = regular_directory(source) and regular_directory(venv)
from_scratch = (
    generation is None
    and revision is None
    and not regular_directory(source)
    and not regular_directory(venv)
)
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
    "rollback_capable": rollback_capable,
    "install_kind": "from_scratch" if from_scratch else "upgrade",
    "rollback_ineligible_reason": (
        None
        if rollback_capable
        else (
            "node has never been deployed; there is no prior generation to restore"
            if from_scratch
            else "prior source and virtualenv generation is incomplete"
        )
    ),
    "artifacts": {
        "source": {"path": str(source), "regular_directory": regular_directory(source)},
        "venv": {"path": str(venv), "regular_directory": regular_directory(venv)},
    },
    "prerequisites": {
        "python": sys.executable,
        "github_cli": shutil.which("gh", path=trusted_command_path),
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
  rm -f "$DAEMON_FUNCTIONS_SNAPSHOT" "$SUPERVISOR_COMPENSATE_PROOF"
}
trap cleanup_phase1_proof EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

export AGENT FLEET_NAME OS_KIND DEPLOY_REV DEPLOY_GENERATION MAC_HOME PY ACTION
export MAC_PHASE1_SUPERVISOR_KIND="$SUPERVISOR_KIND"
export MAC_PHASE1_SUPERVISOR_PROOF_PATH="$SUPERVISOR_PROOF"
export MAC_PHASE1_SUPERVISOR_COMPENSATE_PROOF_PATH="$SUPERVISOR_COMPENSATE_PROOF"
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
host_automation = payload.get("host_automation")
if (
    not isinstance(host_automation, dict)
    or host_automation.get("schema") != "mac.phase1_host_automation.v1"
    or not isinstance(host_automation.get("definitions"), list)
):
    raise SystemExit("phase-1 prepare failed: existing host automation journal is malformed")
for index, item in enumerate(host_automation["definitions"]):
    if not isinstance(item, dict):
        raise SystemExit("phase-1 prepare failed: existing host automation journal is malformed")
    validate_artifact(
        {"path": item.get("backup"), "sha256": item.get("sha256")},
        0o600,
        "host automation backup %d" % index,
    )
if payload["restore_executable"].get("argv") != [
    payload["restore_executable"]["path"],
    "restore",
]:
    raise SystemExit("phase-1 prepare failed: existing restore invocation is malformed")
sys.stdout.buffer.write(raw)
PY
  exit 0
fi

if [ "$ACTION" = restore ] || [ "$ACTION" = resume_media ]; then
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
host_automation = contract.get("host_automation")
definitions = host_automation.get("definitions") if isinstance(host_automation, dict) else None
if (
    not isinstance(host_automation, dict)
    or host_automation.get("schema") != "mac.phase1_host_automation.v1"
    or not isinstance(definitions, list)
):
    raise SystemExit("phase-1 restore failed: host automation journal is malformed")
for item in definitions:
    if (
        not isinstance(item, dict)
        or not isinstance(item.get("backup"), str)
        or not isinstance(item.get("sha256"), str)
        or not isinstance(item.get("size"), int)
    ):
        raise SystemExit("phase-1 restore failed: host automation journal is malformed")
    backup = private_bytes(item["backup"], 0o600, "host automation backup", 1024 * 1024)
    if (
        hashlib.sha256(backup).hexdigest() != item["sha256"]
        or len(backup) != item["size"]
    ):
        raise SystemExit("phase-1 restore failed: host automation backup differs")
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
# Run one bounded supervisor operation (prepare/quiesce/restore/resume, or a
# quiesce compensation) in a single Python process.  Wrapped in a function so
# the quiesce path can re-run it to restore the exact prior topology when the
# downstream daemon-resource gate rejects the deployment.
run_supervisor_phase1_operation() {
  "$PY" - <<'PY'
from __future__ import annotations

import hashlib
import http.client
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
# A quiescence gate that rejects deployment (e.g. an active lease-owned
# OpenShell task sandbox) runs AFTER this process has already stopped the
# worker/gateway services.  When the caller re-invokes this block with the
# compensation flag it must put every service back to its exact prepared
# prior_state -- restarting only what was running and leaving already-stopped
# services stopped -- so a rejected attempt never strands mac-agent STOPPED.
supervisor_compensate = (
    action == "quiesce"
    and os.environ.get("MAC_PHASE1_SUPERVISOR_COMPENSATE") == "1"
)
restore_contract_path = Path(os.environ["MAC_PHASE1_RESTORE_CONTRACT_PATH"])
restore_artifact_dir = Path(os.environ["MAC_PHASE1_RESTORE_ARTIFACT_DIR"])

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
    "MAC_PHASE1_TOTAL_TIMEOUT_SECONDS",
    1200.0 if action == "resume_media" else 600.0,
    0.1,
    1800.0,
)
media_readiness_seconds = bounded_number(
    "MAC_PHASE1_MEDIA_READINESS_SECONDS", 900.0, 0.1, 1800.0
)
test_media_health_max_attempts = None
if os.environ.get("MAC_PHASE1_TEST_MODE") == "1":
    raw_test_attempts = os.environ.get("MAC_PHASE1_TEST_MEDIA_HEALTH_MAX_ATTEMPTS")
    if raw_test_attempts is not None:
        try:
            test_media_health_max_attempts = int(raw_test_attempts)
        except (TypeError, ValueError):
            raise QuiescenceFailure("invalid phase-1 test media health attempt bound")
        if not 1 <= test_media_health_max_attempts <= 100:
            raise QuiescenceFailure("phase-1 test media health attempt bound is outside its range")
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


def systemd_snapshot(prefix: list[str], systemctl: str, unit: str) -> dict[str, object]:
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
            "--property=NRestarts",
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
    if set(values) != {
        "LoadState",
        "ActiveState",
        "SubState",
        "MainPID",
        "NRestarts",
    }:
        raise QuiescenceFailure("systemd service inspection was incomplete")
    try:
        pid = int(values["MainPID"])
        restarts = int(values["NRestarts"])
    except (TypeError, ValueError):
        raise QuiescenceFailure("systemd service counters were malformed")
    if pid < 0 or restarts < 0:
        raise QuiescenceFailure("systemd service counters were invalid")
    if values["LoadState"] == "not-found":
        if values["ActiveState"] != "inactive" or pid != 0:
            raise QuiescenceFailure("systemd reported a contradictory absent service")
        return {"state": "absent", "pid": 0, "restarts": 0}
    if values["ActiveState"] in {"inactive", "failed"}:
        if pid != 0:
            raise QuiescenceFailure("systemd reported an inactive service with a live process")
        return {"state": "inactive", "pid": 0, "restarts": restarts}
    # Every bounded transition is operationally active for quiescence. A
    # crash-looping unit can move through activating/auto-restart,
    # activating/start and deactivating/stop-* between two adjacent show calls;
    # choosing one transient tuple made a real drain depend on sampling luck.
    # Stop the unit and prove an inactive snapshot instead. Stable running
    # services retain the stricter positive-PID contract below.
    if (
        values["LoadState"] in {"loaded", "masked"}
        and values["ActiveState"]
        in {"activating", "deactivating", "reloading", "refreshing", "maintenance"}
    ):
        return {"state": "active", "pid": pid, "restarts": restarts}
    if (
        values["LoadState"] not in {"loaded", "masked"}
        or values["ActiveState"] != "active"
        or values["SubState"] != "running"
        or pid <= 0
    ):
        raise QuiescenceFailure("systemd service has an unexpected load state")
    return {"state": "active", "pid": pid, "restarts": restarts}


def systemd_state(prefix: list[str], systemctl: str, unit: str) -> str:
    return str(systemd_snapshot(prefix, systemctl, unit)["state"])


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
                {
                    "name": unit,
                    "prior_state": state,
                    "state": state,
                    "enabled_state": systemd_enabled_state(prefix, systemctl, unit),
                }
                for unit, state in media_states
            ],
        },
        prefix,
        systemctl,
        [state for _unit, state in prior_states],
    )


def quiesce_systemd() -> dict[str, object]:
    supervisor, prefix, systemctl, prior_values = inspect_systemd()
    del prior_values
    for key in ("resources", "media_resources"):
        resources = supervisor.get(key)
        if not isinstance(resources, list):
            raise QuiescenceFailure("systemd topology lacks a resource group")
        for resource in resources:
            if not isinstance(resource, dict):
                raise QuiescenceFailure("systemd topology has an invalid resource")
            unit = str(resource["name"])
            state = str(resource.get("prior_state") or "")
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


def restore_systemd_resources(
    resources: object,
    prefix: list[str],
    systemctl: str,
    *,
    allow_absent: bool,
) -> None:
    if not isinstance(resources, list):
        raise QuiescenceFailure("restore contract lacks systemd resources")
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
        if prior == "absent":
            if not allow_absent or enabled != "not-found":
                raise QuiescenceFailure(
                    "restore contract has contradictory absent systemd intent"
                )
            if systemd_state(prefix, systemctl, unit) != "absent":
                raise QuiescenceFailure(
                    "successor created a systemd identity absent from the contract"
                )
            continue
        # A successor may have changed the persistent enablement links even
        # when the old process topology was successfully quiesced.  Clear a
        # successor mask before reconstructing activity; reapply the prior
        # mask only after an active-but-masked service has been restarted.
        if run_bounded(prefix + [systemctl, "unmask", unit]).returncode != 0:
            raise QuiescenceFailure("restoring systemd mask intent failed")
        if prior == "active":
            if run_bounded(prefix + [systemctl, "start", unit]).returncode != 0:
                raise QuiescenceFailure("restoring active systemd service failed")
        elif prior == "inactive":
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


def restore_systemd(expected: dict[str, object]) -> dict[str, object]:
    systemctl = command_path("systemctl")
    prefix = privileged(systemctl)[:-1]
    resources = expected.get("resources")
    media_resources = expected.get("media_resources")
    if run_bounded(prefix + [systemctl, "daemon-reload"]).returncode != 0:
        raise QuiescenceFailure("systemd definition reload failed during restore")
    restore_systemd_resources(resources, prefix, systemctl, allow_absent=True)
    restore_systemd_resources(media_resources, prefix, systemctl, allow_absent=True)
    restored, _prefix, _systemctl, _states = inspect_systemd()
    for key, expected_group in (
        ("resources", resources),
        ("media_resources", media_resources),
    ):
        restored_resources = restored.get(key)
        assert isinstance(restored_resources, list)
        assert isinstance(expected_group, list)
        expected_by_name = {
            str(item["name"]): item
            for item in expected_group
            if isinstance(item, dict)
        }
        for item in restored_resources:
            expected_item = expected_by_name.get(str(item["name"]))
            if expected_item is None:
                raise QuiescenceFailure(
                    "restored systemd topology has an unexpected identity"
                )
            if (
                item.get("prior_state") != expected_item.get("prior_state")
                or item.get("enabled_state") != expected_item.get("enabled_state")
            ):
                raise QuiescenceFailure(
                    "restored systemd topology differs from its contract"
                )
            item["state"] = item["prior_state"]
    return restored


def media_health_ports() -> dict[str, int]:
    path = Path(os.environ["MAC_HOME"]) / "mac.env"
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        raise QuiescenceFailure("media readiness environment is unavailable")
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or stat.S_IMODE(before.st_mode) & 0o077
            or before.st_size <= 0
            or before.st_size > 4 * 1024 * 1024
        ):
            raise QuiescenceFailure(
                "media readiness environment is not owner-private and bounded"
            )
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
            raise QuiescenceFailure("media readiness environment changed while reading")
    finally:
        os.close(descriptor)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise QuiescenceFailure("media readiness environment is malformed")
    values: dict[str, str] = {}
    admitted = {
        "MAC_AGENT_GEN_PORT",
        "MAC_AGENT_GEN_AUDIO_PORT",
        "MAC_AGENT_GEN_VIDEO_PORT",
    }
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key not in admitted:
            continue
        if key in values:
            raise QuiescenceFailure("media readiness environment is ambiguous")
        values[key] = value.strip().strip("\"'")
    configured = {
        "%s-gen-server.service" % fleet: (
            "MAC_AGENT_GEN_PORT",
            "8189",
        ),
        "%s-gen-audio-server.service" % fleet: (
            "MAC_AGENT_GEN_AUDIO_PORT",
            "8190",
        ),
        "%s-gen-video-server.service" % fleet: (
            "MAC_AGENT_GEN_VIDEO_PORT",
            "8191",
        ),
    }
    ports: dict[str, int] = {}
    for unit, (key, default) in configured.items():
        raw_port = values.get(key, default)
        try:
            port = int(raw_port)
        except (TypeError, ValueError):
            raise QuiescenceFailure("media readiness port is invalid")
        if not 1 <= port <= 65535:
            raise QuiescenceFailure("media readiness port is outside its bound")
        ports[unit] = port
    return ports


def prove_media_health(unit: str, port: int, health_deadline: float) -> None:
    attempts = 0
    while True:
        connection = None
        try:
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                port,
                timeout=max(0.05, min(command_timeout, health_deadline - time.monotonic())),
            )
            connection.request("GET", "/health")
            response = connection.getresponse()
            raw = response.read(64 * 1024 + 1)
            payload = json.loads(raw)
            if (
                response.status == 200
                and len(raw) <= 64 * 1024
                and isinstance(payload, dict)
                and payload.get("ok") is True
            ):
                return
        except (OSError, ValueError, json.JSONDecodeError, http.client.HTTPException):
            pass
        finally:
            if connection is not None:
                connection.close()
        attempts += 1
        if (
            test_media_health_max_attempts is not None
            and attempts >= test_media_health_max_attempts
        ):
            raise QuiescenceFailure(
                "resumed media service did not pass its bounded health check"
            )
        remaining = min(deadline, health_deadline) - time.monotonic()
        if remaining <= 0:
            raise QuiescenceFailure(
                "resumed media service did not pass its bounded health check"
            )
        time.sleep(min(poll_seconds, remaining))


def resume_systemd_media(expected: dict[str, object]) -> dict[str, object]:
    systemctl = command_path("systemctl")
    prefix = privileged(systemctl)[:-1]
    resources = expected.get("media_resources")
    if run_bounded(prefix + [systemctl, "daemon-reload"]).returncode != 0:
        raise QuiescenceFailure("systemd definition reload failed during media resume")
    restore_systemd_resources(resources, prefix, systemctl, allow_absent=True)
    assert isinstance(resources, list)
    ports = media_health_ports()
    health_deadline = min(deadline, time.monotonic() + media_readiness_seconds)
    samples: list[dict[str, dict[str, object]]] = []
    for observation in range(2):
        sample: dict[str, dict[str, object]] = {}
        for item in resources:
            if not isinstance(item, dict):
                raise QuiescenceFailure("media restore contract has an invalid resource")
            unit = str(item.get("name") or "")
            current = systemd_snapshot(prefix, systemctl, unit)
            enabled = systemd_enabled_state(prefix, systemctl, unit)
            if (
                current.get("state") != item.get("prior_state")
                or enabled != item.get("enabled_state")
            ):
                raise QuiescenceFailure(
                    "resumed media topology differs from its phase-1 contract"
                )
            if item.get("prior_state") == "active":
                prove_media_health(unit, ports[unit], health_deadline)
            sample[unit] = current
        samples.append(sample)
        if observation == 0:
            pause()
    for item in resources:
        assert isinstance(item, dict)
        unit = str(item["name"])
        if item.get("prior_state") == "active" and (
            samples[0][unit].get("pid"),
            samples[0][unit].get("restarts"),
        ) != (
            samples[1][unit].get("pid"),
            samples[1][unit].get("restarts"),
        ):
            raise QuiescenceFailure("resumed media service was unstable")
    return {
        "manager": "systemd",
        "media_resources": [
            {
                **item,
                "state": item.get("prior_state"),
                "stable_observations": 2,
            }
            for item in resources
            if isinstance(item, dict)
        ],
    }


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


def host_automation_pattern(manager: str) -> re.Pattern[str] | None:
    escaped = re.escape(fleet)
    if manager == "systemd":
        return re.compile(
            r"%s-openclaw-script-[a-z0-9][a-z0-9-]*\.(?:service|timer)\Z"
            % escaped
        )
    if manager == "launchd":
        return re.compile(
            r"com\.%s\.openclaw-script-[a-z0-9][a-z0-9-]*\.plist\Z"
            % escaped
        )
    return None


def host_automation_definition_directory(manager: str) -> Path | None:
    if manager == "systemd":
        return Path.home() / ".config" / "systemd" / "user"
    if manager == "launchd":
        return Path.home() / "Library" / "LaunchAgents"
    return None


def safe_definition_directory(directory: Path, *, required: bool = False) -> bool:
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        if required:
            raise QuiescenceFailure("OpenClaw host automation directory is unavailable")
        return False
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o022
    ):
        raise QuiescenceFailure("OpenClaw host automation directory is unsafe")
    return True


def stable_definition_bytes(path: Path, *, backup: bool = False) -> tuple[bytes, int]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(str(path), flags)
    except OSError:
        raise QuiescenceFailure("OpenClaw host automation definition is unsafe")
    try:
        before = os.fstat(descriptor)
        mode = stat.S_IMODE(before.st_mode)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_uid != os.getuid()
            or before.st_nlink != 1
            or (mode != 0o600 if backup else bool(mode & 0o022))
            or before.st_size <= 0
            or before.st_size > 1024 * 1024
        ):
            raise QuiescenceFailure("OpenClaw host automation definition is unsafe")
        raw = bytearray()
        while len(raw) < before.st_size:
            chunk = os.read(descriptor, min(64 * 1024, before.st_size - len(raw)))
            if not chunk:
                raise QuiescenceFailure("OpenClaw host automation definition was truncated")
            raw.extend(chunk)
        if os.read(descriptor, 1):
            raise QuiescenceFailure("OpenClaw host automation definition grew while reading")
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
            raise QuiescenceFailure("OpenClaw host automation definition changed while reading")
        return bytes(raw), mode
    finally:
        os.close(descriptor)


def host_automation_definitions(manager: str) -> list[Path]:
    directory = host_automation_definition_directory(manager)
    pattern = host_automation_pattern(manager)
    if directory is None or pattern is None or not safe_definition_directory(directory):
        return []
    definitions = sorted(
        (entry for entry in directory.iterdir() if pattern.fullmatch(entry.name)),
        key=lambda entry: entry.name,
    )
    for entry in definitions:
        stable_definition_bytes(entry)
    if manager == "systemd":
        wants = directory / "timers.target.wants"
        if safe_definition_directory(wants):
            timer_pattern = re.compile(
                r"%s-openclaw-script-[a-z0-9][a-z0-9-]*\.timer\Z"
                % re.escape(fleet)
            )
            by_name = {path.name: path for path in definitions}
            for entry in wants.iterdir():
                if timer_pattern.fullmatch(entry.name) is None:
                    continue
                try:
                    metadata = entry.lstat()
                    target = (entry.parent / os.readlink(entry)).resolve(strict=True)
                except OSError:
                    raise QuiescenceFailure("OpenClaw host automation enablement link is unsafe")
                expected = by_name.get(entry.name)
                if (
                    not stat.S_ISLNK(metadata.st_mode)
                    or metadata.st_uid != os.getuid()
                    or expected is None
                    or target != expected.resolve(strict=True)
                ):
                    raise QuiescenceFailure("OpenClaw host automation enablement link is unsafe")
    return definitions


def systemd_user_runtime() -> Path:
    runtime_raw = ""
    if os.environ.get("MAC_PHASE1_TEST_MODE") == "1":
        runtime_raw = os.environ.get("FAKE_USER_RUNTIME_DIR", "")
    return Path(runtime_raw or ("/run/user/%d" % os.getuid()))


def systemd_user_context() -> tuple[str, dict[str, str]]:
    runtime = systemd_user_runtime()
    metadata = runtime.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.getuid()
    ):
        raise QuiescenceFailure("systemd user-manager runtime is unsafe")
    environment = dict(clean_env)
    environment["XDG_RUNTIME_DIR"] = str(runtime)
    return command_path("systemctl"), environment


def user_systemctl(argv: list[str]) -> subprocess.CompletedProcess[str]:
    systemctl, environment = systemd_user_context()
    return run_bounded([systemctl, "--user", *argv], environment)


def systemd_user_state(unit: str) -> str:
    result = user_systemctl(
        [
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
        raise QuiescenceFailure("systemd user automation inspection failed")
    values: dict[str, str] = {}
    for line in (result.stdout or "").splitlines():
        if not line or "=" not in line:
            raise QuiescenceFailure("systemd user automation inspection was malformed")
        key, value = line.split("=", 1)
        if key in values:
            raise QuiescenceFailure("systemd user automation inspection was ambiguous")
        values[key] = value
    expected = {"LoadState", "ActiveState", "SubState", "MainPID"}
    if set(values) == expected - {"MainPID"} and unit.endswith(".timer"):
        # systemd timers have no service process and older systemd releases
        # omit the nonexistent MainPID property instead of rendering it as 0.
        values["MainPID"] = "0"
    if set(values) != expected:
        raise QuiescenceFailure("systemd user automation inspection was incomplete")
    if values["LoadState"] == "not-found":
        return "absent"
    if values["ActiveState"] in {"inactive", "failed"}:
        if values["MainPID"] != "0":
            raise QuiescenceFailure("systemd user automation has an unclassified process")
        return "inactive"
    if values["LoadState"] not in {"loaded", "masked"}:
        raise QuiescenceFailure("systemd user automation has an unexpected load state")
    return "active"


def systemd_user_enabled(unit: str) -> str:
    result = user_systemctl(["is-enabled", unit])
    value = (result.stdout or "").strip()
    if value not in {"enabled", "disabled", "masked", "static", "indirect", "not-found"}:
        raise QuiescenceFailure("systemd user automation enablement is invalid")
    if result.returncode not in {0, 1, 3, 4}:
        raise QuiescenceFailure("systemd user automation enablement inspection failed")
    return value


def systemd_loaded_automation() -> set[str]:
    pattern = host_automation_pattern("systemd")
    assert pattern is not None
    result = user_systemctl(
        [
            "list-units",
            "--all",
            "--no-pager",
            "--no-legend",
            "--plain",
            "%s-openclaw-script-*.service" % fleet,
            "%s-openclaw-script-*.timer" % fleet,
        ]
    )
    if result.returncode != 0:
        raise QuiescenceFailure("systemd user-manager automation inventory failed")
    loaded: set[str] = set()
    for line in (result.stdout or "").splitlines():
        fields = line.split()
        if not fields or pattern.fullmatch(fields[0]) is None or fields[0] in loaded:
            raise QuiescenceFailure("systemd user-manager automation inventory was malformed")
        loaded.add(fields[0])
    return loaded


def launchd_loaded_automation() -> set[str]:
    launchctl = command_path("launchctl")
    # ``launchctl print gui/<uid>`` serializes the complete launchd domain,
    # including every job's environment and endpoint state.  A healthy desktop
    # can easily exceed the supervisor command bound (rocky was 117 KiB with
    # only three matching automation jobs), and none of that unrelated detail
    # is needed here.  ``launchctl list`` is the compact loaded-job inventory:
    # one PID/status/label row per job and no job environment.  Parse only exact
    # managed labels, while still rejecting a malformed matching row or a
    # duplicate so an ambiguous inventory cannot authorize rollback.
    result = run_bounded([launchctl, "list"])
    if result.returncode != 0:
        raise QuiescenceFailure("launchd automation inventory failed")
    label_pattern = re.compile(
        r"com\.%s\.openclaw-script-[a-z0-9][a-z0-9-]*\Z"
        % re.escape(fleet)
    )
    row_pattern = re.compile(
        r"^\s*(?:-|[0-9]+)\s+-?[0-9]+\s+([A-Za-z0-9._-]+)\s*$"
    )
    loaded: set[str] = set()
    for line in (result.stdout or "").splitlines():
        if line.strip().split() == ["PID", "Status", "Label"]:
            continue
        match = row_pattern.fullmatch(line)
        if match is None:
            if "com.%s.openclaw-script-" % fleet in line:
                raise QuiescenceFailure("launchd automation inventory was malformed")
            continue
        label = match.group(1)
        if label_pattern.fullmatch(label) is None:
            continue
        if label in loaded:
            raise QuiescenceFailure("launchd automation inventory was ambiguous")
        loaded.add(label)
    return loaded


def launchd_dynamic_disabled(labels: set[str]) -> dict[str, bool]:
    if not labels:
        return {}
    launchctl = command_path("launchctl")
    domain = "gui/%d" % os.getuid()
    result = run_bounded([launchctl, "print-disabled", domain])
    if result.returncode != 0:
        raise QuiescenceFailure("launchd automation disable-override inventory failed")
    parsed: dict[str, bool] = {}
    pattern = re.compile(
        r'^\s*"([A-Za-z0-9._-]+)"\s*=>\s*(enabled|disabled|true|false)\s*$'
    )
    text = result.stdout or ""
    for line in text.splitlines():
        match = pattern.fullmatch(line)
        if match is None:
            continue
        label, value = match.groups()
        if label in labels:
            if label in parsed:
                raise QuiescenceFailure("launchd automation disable override is ambiguous")
            parsed[label] = value in {"disabled", "true"}
    for label in labels:
        if label in text and label not in parsed:
            raise QuiescenceFailure("launchd automation disable override is malformed")
    return {label: parsed.get(label, False) for label in labels}


def write_host_automation_backup(path: Path, destination: Path) -> tuple[str, str, int]:
    raw, mode = stable_definition_bytes(path)
    descriptor = os.open(str(destination), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise QuiescenceFailure("OpenClaw host automation backup was truncated")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return hashlib.sha256(raw).hexdigest(), format(mode, "04o"), len(raw)


def prepare_host_automation(manager: str) -> dict[str, object]:
    if manager not in {"systemd", "launchd"}:
        return {"schema": "mac.phase1_host_automation.v1", "manager": manager, "definitions": []}
    definitions = host_automation_definitions(manager)
    names = {path.name for path in definitions}
    if manager == "systemd":
        loaded = (
            systemd_loaded_automation()
            if definitions or systemd_user_runtime().exists()
            else set()
        )
        if not loaded.issubset(names):
            raise QuiescenceFailure("loaded OpenClaw host automation lacks its exact definition")
        disabled: dict[str, bool] = {}
    else:
        labels = {path.name.removesuffix(".plist") for path in definitions}
        loaded = launchd_loaded_automation()
        if not loaded.issubset(labels):
            raise QuiescenceFailure("loaded OpenClaw host automation lacks its exact definition")
        disabled = launchd_dynamic_disabled(labels)
    backup_root = restore_artifact_dir / "host-automation"
    if backup_root.exists() or backup_root.is_symlink():
        raise QuiescenceFailure("OpenClaw host automation backup root appeared concurrently")
    backup_root.mkdir(mode=0o700)
    entries: list[dict[str, object]] = []
    for path in definitions:
        backup = backup_root / path.name
        digest, mode, size = write_host_automation_backup(path, backup)
        if manager == "systemd":
            prior_state = systemd_user_state(path.name)
            if path.name.endswith(".service") and prior_state == "active":
                raise QuiescenceFailure(
                    "active OpenClaw host automation service is not replay-safe"
                )
            entry = {
                "name": path.name,
                "path": str(path),
                "backup": str(backup),
                "sha256": digest,
                "size": size,
                "mode": mode,
                "prior_state": prior_state,
                "state": prior_state,
                "enabled_state": systemd_user_enabled(path.name),
            }
        else:
            label = path.name.removesuffix(".plist")
            prior_state = "active" if label in loaded else "absent"
            entry = {
                "name": label,
                "path": str(path),
                "backup": str(backup),
                "sha256": digest,
                "size": size,
                "mode": mode,
                "prior_state": prior_state,
                "state": prior_state,
                "disabled_override": disabled[label],
            }
        entries.append(entry)
    directory = os.open(str(backup_root), os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return {
        "schema": "mac.phase1_host_automation.v1",
        "manager": manager,
        "definitions": entries,
    }


def expected_host_entries(expected: dict[str, object], manager: str) -> list[dict[str, object]]:
    if (
        expected.get("schema") != "mac.phase1_host_automation.v1"
        or expected.get("manager") != manager
        or not isinstance(expected.get("definitions"), list)
    ):
        raise QuiescenceFailure("restore contract lacks exact host automation")
    entries = expected["definitions"]
    assert isinstance(entries, list)
    result: list[dict[str, object]] = []
    names: set[str] = set()
    definition_directory = host_automation_definition_directory(manager)
    pattern = host_automation_pattern(manager)
    backup_root = restore_artifact_dir / "host-automation"
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
            raise QuiescenceFailure("restore contract has invalid host automation")
        if entry["name"] in names:
            raise QuiescenceFailure("restore contract has duplicate host automation")
        path = Path(str(entry.get("path") or ""))
        backup = Path(str(entry.get("backup") or ""))
        expected_filename = (
            str(entry["name"])
            if manager == "systemd"
            else str(entry["name"]) + ".plist"
        )
        if (
            definition_directory is None
            or pattern is None
            or path.parent != definition_directory
            or path.name != expected_filename
            or pattern.fullmatch(path.name) is None
            or backup.parent != backup_root
            or backup.name != path.name
        ):
            raise QuiescenceFailure("restore contract has invalid host automation paths")
        backup_raw, _backup_mode = stable_definition_bytes(backup, backup=True)
        if (
            hashlib.sha256(backup_raw).hexdigest() != entry.get("sha256")
            or len(backup_raw) != entry.get("size")
        ):
            raise QuiescenceFailure("OpenClaw host automation backup differs from contract")
        names.add(str(entry["name"]))
        result.append(entry)
    return result


def verify_definition_set(expected: dict[str, object], manager: str) -> list[dict[str, object]]:
    entries = expected_host_entries(expected, manager)
    paths = host_automation_definitions(manager)
    expected_paths = {str(item.get("path")): item for item in entries}
    if {str(path) for path in paths} != set(expected_paths):
        raise QuiescenceFailure("OpenClaw host automation definitions changed after prepare")
    for path in paths:
        raw, mode = stable_definition_bytes(path)
        item = expected_paths[str(path)]
        if (
            hashlib.sha256(raw).hexdigest() != item.get("sha256")
            or len(raw) != item.get("size")
            or format(mode, "04o") != item.get("mode")
        ):
            raise QuiescenceFailure("OpenClaw host automation definition changed after prepare")
    return entries


def quiesce_host_automation(expected: dict[str, object], manager: str) -> dict[str, object]:
    entries = verify_definition_set(expected, manager)
    result = json.loads(json.dumps(expected))
    result_entries = result["definitions"]
    assert isinstance(result_entries, list)
    if manager == "systemd":
        loaded = (
            systemd_loaded_automation()
            if entries or systemd_user_runtime().exists()
            else set()
        )
        if not loaded.issubset({str(item["name"]) for item in entries}):
            raise QuiescenceFailure("OpenClaw host automation state changed after prepare")
        for item in result_entries:
            unit = str(item["name"])
            current = systemd_user_state(unit)
            if current != item.get("prior_state"):
                raise QuiescenceFailure("OpenClaw host automation state changed after prepare")
            if current == "active":
                stop_systemd_user_unit(unit)
            if unit.endswith(".timer"):
                if user_systemctl(["disable", unit]).returncode != 0:
                    raise QuiescenceFailure("disabling OpenClaw host automation failed")
                if systemd_user_enabled(unit) != "disabled":
                    raise QuiescenceFailure("OpenClaw host automation remained enabled")
            final = systemd_user_state(unit)
            if final not in {"inactive", "absent"}:
                raise QuiescenceFailure("OpenClaw host automation did not quiesce")
            item["state"] = final
    elif manager == "launchd":
        loaded = launchd_loaded_automation()
        expected_loaded = {
            str(item["name"])
            for item in entries
            if item.get("prior_state") == "active"
        }
        if loaded != expected_loaded:
            raise QuiescenceFailure("OpenClaw host automation state changed after prepare")
        launchctl = command_path("launchctl")
        uid = os.getuid()
        for item in result_entries:
            label = str(item["name"])
            target = "gui/%d/%s" % (uid, label)
            if item.get("prior_state") == "active":
                if run_bounded([launchctl, "bootout", target]).returncode != 0:
                    raise QuiescenceFailure("OpenClaw host automation bootout failed")
            if launchd_state([], launchctl, target) != "absent":
                raise QuiescenceFailure("OpenClaw host automation did not quiesce")
            item["state"] = "absent"
    return result


def stop_systemd_user_unit(unit: str) -> None:
    if user_systemctl(["stop", unit]).returncode != 0:
        raise QuiescenceFailure("stopping OpenClaw host automation failed")
    while True:
        state = systemd_user_state(unit)
        if state in {"inactive", "absent"}:
            return
        pause()


def remove_current_host_automation(manager: str) -> None:
    paths = host_automation_definitions(manager)
    if manager == "systemd":
        if not systemd_user_runtime().exists():
            for path in paths:
                path.unlink()
            return
        names = {path.name for path in paths} | systemd_loaded_automation()
        for unit in sorted(names):
            if systemd_user_state(unit) == "active":
                stop_systemd_user_unit(unit)
            if unit.endswith(".timer"):
                if user_systemctl(["disable", unit]).returncode != 0:
                    raise QuiescenceFailure("disabling successor host automation failed")
        for path in paths:
            path.unlink()
        if user_systemctl(["daemon-reload"]).returncode != 0:
            raise QuiescenceFailure("reloading systemd user automation failed")
    elif manager == "launchd":
        launchctl = command_path("launchctl")
        uid = os.getuid()
        labels = {path.name.removesuffix(".plist") for path in paths}
        labels |= launchd_loaded_automation()
        for label in sorted(labels):
            target = "gui/%d/%s" % (uid, label)
            if launchd_state([], launchctl, target) == "active":
                if run_bounded([launchctl, "bootout", target]).returncode != 0:
                    raise QuiescenceFailure("removing successor host automation failed")
                while launchd_state([], launchctl, target) != "absent":
                    pause()
            if run_bounded([launchctl, "enable", target]).returncode != 0:
                raise QuiescenceFailure("clearing successor host automation override failed")
        for path in paths:
            path.unlink()


def restore_definition(item: dict[str, object]) -> None:
    destination = Path(str(item.get("path") or ""))
    backup = Path(str(item.get("backup") or ""))
    raw, backup_mode = stable_definition_bytes(backup, backup=True)
    if (
        hashlib.sha256(raw).hexdigest() != item.get("sha256")
        or len(raw) != item.get("size")
        or backup_mode != 0o600
        or not isinstance(item.get("mode"), str)
        or re.fullmatch(r"0[0-7]{3}", str(item["mode"])) is None
    ):
        raise QuiescenceFailure("OpenClaw host automation backup differs from contract")
    safe_definition_directory(destination.parent, required=True)
    descriptor, temporary_raw = tempfile.mkstemp(
        prefix="." + destination.name + ".phase1-restore.", dir=str(destination.parent)
    )
    temporary = Path(temporary_raw)
    try:
        os.fchmod(descriptor, int(str(item["mode"]), 8))
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
        directory = os.open(str(destination.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def restore_host_automation(expected: dict[str, object], manager: str) -> dict[str, object]:
    entries = expected_host_entries(expected, manager)
    remove_current_host_automation(manager)
    for item in entries:
        restore_definition(item)
    if manager == "systemd":
        if entries or systemd_user_runtime().exists():
            if user_systemctl(["daemon-reload"]).returncode != 0:
                raise QuiescenceFailure("reloading restored systemd user automation failed")
        for item in entries:
            unit = str(item["name"])
            enabled = item.get("enabled_state")
            if enabled == "enabled":
                if user_systemctl(["enable", unit]).returncode != 0:
                    raise QuiescenceFailure("restoring host automation enablement failed")
            elif enabled == "disabled":
                if user_systemctl(["disable", unit]).returncode != 0:
                    raise QuiescenceFailure("restoring host automation disablement failed")
            elif enabled not in {"static", "indirect", "not-found"}:
                raise QuiescenceFailure("host automation enablement is not exactly restorable")
            if item.get("prior_state") == "active":
                if user_systemctl(["start", unit]).returncode != 0:
                    raise QuiescenceFailure("restoring host automation runtime failed")
    elif manager == "launchd":
        launchctl = command_path("launchctl")
        uid = os.getuid()
        domain = "gui/%d" % uid
        for item in entries:
            label = str(item["name"])
            target = "%s/%s" % (domain, label)
            if item.get("prior_state") == "active":
                if run_bounded([launchctl, "bootstrap", domain, str(item["path"])]).returncode != 0:
                    raise QuiescenceFailure("restoring host automation launchd job failed")
            action_name = "disable" if item.get("disabled_override") is True else "enable"
            if run_bounded([launchctl, action_name, target]).returncode != 0:
                raise QuiescenceFailure("restoring host automation launchd override failed")
    restored = prepare_host_automation_state(manager, entries)
    expected_initial = json.loads(json.dumps(expected))
    if restored != expected_initial:
        raise QuiescenceFailure("restored host automation differs from its contract")
    return restored


def prepare_host_automation_state(
    manager: str, entries: list[dict[str, object]]
) -> dict[str, object]:
    paths = host_automation_definitions(manager)
    if {str(path) for path in paths} != {str(item.get("path")) for item in entries}:
        raise QuiescenceFailure("restored host automation definition set differs")
    result = json.loads(
        json.dumps(
            {
                "schema": "mac.phase1_host_automation.v1",
                "manager": manager,
                "definitions": entries,
            }
        )
    )
    result_entries = result["definitions"]
    assert isinstance(result_entries, list)
    if manager == "systemd":
        if not entries and not systemd_user_runtime().exists():
            return result
        for item in result_entries:
            item["state"] = systemd_user_state(str(item["name"]))
            if systemd_user_enabled(str(item["name"])) != item.get("enabled_state"):
                raise QuiescenceFailure("restored host automation enablement differs")
    elif manager == "launchd":
        loaded = launchd_loaded_automation()
        disabled = launchd_dynamic_disabled({str(item["name"]) for item in result_entries})
        for item in result_entries:
            item["state"] = "active" if str(item["name"]) in loaded else "absent"
            if disabled[str(item["name"])] != item.get("disabled_override"):
                raise QuiescenceFailure("restored host automation override differs")
    for item in result_entries:
        raw, mode = stable_definition_bytes(Path(str(item["path"])))
        if (
            hashlib.sha256(raw).hexdigest() != item.get("sha256")
            or len(raw) != item.get("size")
            or format(mode, "04o") != item.get("mode")
        ):
            raise QuiescenceFailure("restored host automation definition differs")
    return result


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
        for resource in group.get("media_resources") or []:
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
    if action == "prepare":
        host_automation = prepare_host_automation(manager)
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
        expected_host_automation = contract.get("host_automation")
        assert isinstance(expected_supervisor, dict)
        if not isinstance(expected_host_automation, dict):
            raise QuiescenceFailure("prepared contract lacks host automation journal")
        if expected_supervisor.get("manager") != manager:
            raise QuiescenceFailure("prepared supervisor manager differs from requested manager")
        if action == "quiesce" and supervisor_compensate:
            # Compensate the services this same attempt stopped: restore each
            # to its prepared prior_state (start what was running, leave what
            # was stopped stopped).  Reuses the exact restore path so the
            # compensation is byte-for-byte the topology the contract promises.
            host_automation = restore_host_automation(
                expected_host_automation, manager
            )
            if manager == "systemd":
                supervisor = restore_systemd(expected_supervisor)
            elif manager == "launchd":
                supervisor = restore_launchd(expected_supervisor)
            elif manager == "supervisord":
                supervisor = restore_supervisord(expected_supervisor)
            else:
                raise QuiescenceFailure("unsupported supervisor kind")
            if initial_topology(supervisor) != expected_supervisor:
                raise QuiescenceFailure(
                    "compensated supervisor topology differs from its contract"
                )
            proof_schema = "mac.phase1_supervisor_compensation.v1"
        elif action == "quiesce":
            host_automation = quiesce_host_automation(
                expected_host_automation, manager
            )
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
            host_automation = restore_host_automation(
                expected_host_automation, manager
            )
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
        elif action == "resume_media":
            host_automation = expected_host_automation
            if manager == "systemd":
                supervisor = resume_systemd_media(expected_supervisor)
            else:
                if expected_supervisor.get("media_resources") not in (None, []):
                    raise QuiescenceFailure(
                        "non-systemd restore contract contains media services"
                    )
                supervisor = {"manager": manager, "media_resources": []}
            proof_schema = "mac.phase1_supervisor_media_resume.v1"
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
        "host_automation": host_automation,
    }
    if action == "resume_media":
        proof["source_contract_sha256"] = os.environ.get(
            "MAC_PHASE1_RESTORE_CONTRACT_SHA256", ""
        )
    if supervisor_compensate:
        atomic_private_json(
            Path(os.environ["MAC_PHASE1_SUPERVISOR_COMPENSATE_PROOF_PATH"]), proof
        )
    else:
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
}

run_supervisor_phase1_operation

if [ "$ACTION" = resume_media ]; then
  printf 'phase-1 media resume complete: agent=%s generation=%s proof=%s\n' \
    "$AGENT" "$DEPLOY_GENERATION" "$SUPERVISOR_PROOF"
  exit 0
fi

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
host_automation = supervisor_proof.get("host_automation")
if (
    not isinstance(host_automation, dict)
    or host_automation.get("schema") != "mac.phase1_host_automation.v1"
    or host_automation.get("manager") != supervisor.get("manager")
    or not isinstance(host_automation.get("definitions"), list)
):
    raise SystemExit("phase-1 prepare failed: host automation journal is missing")
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
    "host_automation": host_automation,
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
    or supervisor.get("host_automation") != contract.get("host_automation")
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
    "host_automation": supervisor["host_automation"],
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
# The daemon-resource gate is the point at which an active lease-owned OpenShell
# task sandbox correctly rejects the deployment.  The worker/gateway services
# were already stopped by run_supervisor_phase1_operation above, so a bare exit
# here would strand mac-agent STOPPED.  On any gate failure, compensate by
# restoring every service this attempt changed to its exact prepared prior_state
# (idempotent: restarts only what was running, leaves what was stopped stopped),
# then fail closed with the original rejection.
daemon_gate_rc=0
quiesce_daemon_resources_before_source_replacement || daemon_gate_rc=$?
if [ "$daemon_gate_rc" -ne 0 ]; then
  if MAC_PHASE1_SUPERVISOR_COMPENSATE=1 run_supervisor_phase1_operation; then
    printf '%s\n' \
      "phase-1 quiescence rejected; supervisor restored to its pre-attempt state" >&2
  else
    printf '%s\n' \
      "phase-1 quiescence rejected AND supervisor compensation failed" >&2
  fi
  exit "$daemon_gate_rc"
fi

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
            {"absent", "inactive"},
            {"absent", "inactive", "active"},
        )
        if not all(
            isinstance(item, dict)
            and item.get("enabled_state")
            in {"enabled", "disabled", "masked", "static", "indirect", "not-found"}
            for item in [*resources, *payload.get("media_resources")]
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


def validate_host_automation_payload(payload, contract_payload):
    if (
        not isinstance(payload, dict)
        or payload.get("schema") != "mac.phase1_host_automation.v1"
        or not isinstance(contract_payload, dict)
        or contract_payload.get("schema") != "mac.phase1_host_automation.v1"
        or payload.get("manager") != contract_payload.get("manager")
        or not isinstance(payload.get("definitions"), list)
        or not isinstance(contract_payload.get("definitions"), list)
    ):
        raise ReceiptFailure("host automation proof is malformed")
    manager = payload.get("manager")
    expected = {
        item.get("name"): item
        for item in contract_payload["definitions"]
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if len(expected) != len(contract_payload["definitions"]):
        raise ReceiptFailure("host automation contract has duplicate resources")
    observed = set()
    for item in payload["definitions"]:
        if not isinstance(item, dict) or item.get("name") not in expected:
            raise ReceiptFailure("host automation proof has an unexpected resource")
        name = item["name"]
        if name in observed:
            raise ReceiptFailure("host automation proof has duplicate resources")
        observed.add(name)
        prior = expected[name]
        comparable = dict(item)
        comparable["state"] = comparable.get("prior_state")
        if comparable != prior:
            raise ReceiptFailure("host automation proof differs from its contract")
        final_state = item.get("state")
        if manager == "launchd" and final_state != "absent":
            raise ReceiptFailure("launchd host automation is not quiescent")
        if manager == "systemd" and final_state not in {"absent", "inactive"}:
            raise ReceiptFailure("systemd host automation is not quiescent")
    if observed != set(expected):
        raise ReceiptFailure("host automation proof is incomplete")

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
    openshell_task_sandboxes = daemon.get("openshell_task_sandboxes")
    if (
        not isinstance(openshell_task_sandboxes, dict)
        or openshell_task_sandboxes.get("final_state") != "quiescent"
        or openshell_task_sandboxes.get("stable_inactive_observations") != 2
        or not isinstance(openshell_task_sandboxes.get("reconciled"), list)
    ):
        raise ReceiptFailure("daemon receipt lacks OpenShell task-sandbox quiescence")
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
    validate_host_automation_payload(
        supervisor.get("host_automation"), contract.get("host_automation")
    )
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
        "host_automation": supervisor["host_automation"],
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
